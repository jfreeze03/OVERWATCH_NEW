"""R1-091 / R1-207: Action Center's KPIs are UNCAPPED SQL window totals, not a pandas sum over the LIMIT-500 read.

The read sorted by severity and never by status, so with 'Include completed work' closed CRITICAL / HIGH history
took the 500 slots and pushed open LOW / MEDIUM items out: flipping a display toggle halved "Open work" and
"Estimated opportunity", and a >500-item open queue undercounted with no cap note. Now:

* workbench_sql.action_center(with_kpi_totals=True) emits the KPI counts as window totals computed before the
  LIMIT, with action_summary's own masks (deferred excluded, team placeholders = Unassigned), and with
  include_closed the open work sorts FIRST;
* action_summary reads those totals when the frame carries them;
* the page discloses the list cap; 'Assigned to me' filters in the read itself (v4.608: it was a row filter
  over the capped read, so the viewer's own work past the 500 cap was never listed), and its totals count only
  the viewer's work; that read never depends on the selected item (review fix) -- someone else's open item is
  kept listed from its own one-row read, outside the viewer's counts.

The builder is EXECUTED (sqlglot -> SQLite) against an in-memory ACTION_QUEUE and must agree with
action_summary over the WHOLE population.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest
import sqlglot

from app.data import workbench_sql
from app.data.common import account_today_sql
from app.logic import workbench as logic_wb

_TODAY = date(2026, 9, 30)
_COLS = ("ACTION_ID", "CREATED_AT", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "OWNER", "STATUS", "DUE_DATE",
         "DEFER_UNTIL", "COMPLETED_AT", "RESOLUTION_NOTE", "SOURCE", "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY",
         "CONFIDENCE", "PROOF_SQL", "ESTIMATED_USD", "PERIOD", "UPDATED_AT", "UPDATED_BY")


def _row(i: int, **kw) -> dict:
    base = dict.fromkeys(_COLS)
    base.update(ACTION_ID=f"a{i:04d}", CREATED_AT=f"2026-09-01 00:{i % 60:02d}:00", COMPANY="ALL",
                SEVERITY="LOW", TITLE=f"t{i}", OWNER="", STATUS="OPEN", ESTIMATED_USD=50.0)
    base.update(kw)
    return base


def _queue(extra_open: int = 0) -> list[dict]:
    rows = [_row(i, SEVERITY="CRITICAL", STATUS="DONE", OWNER="JOE", ESTIMATED_USD=100.0) for i in range(300)]
    rows += [_row(300 + i) for i in range(300)]                                   # 300 open LOW, unassigned
    rows += [_row(1000 + i, SEVERITY="MEDIUM", OWNER="BO", ESTIMATED_USD=10.0) for i in range(extra_open)]
    rows += [_row(600 + i, SEVERITY="HIGH", OWNER="ANA", DUE_DATE="2026-09-01") for i in range(5)]  # overdue
    rows += [_row(700 + i, OWNER="DBA", DEFER_UNTIL="2026-10-15") for i in range(4)]               # parked
    rows += [_row(800, OWNER="ANA", DEFER_UNTIL="2026-09-30")]                                      # resumed today
    return rows


def _execute(sql: str, rows: list[dict]) -> pd.DataFrame:
    sql = (sql.replace(account_today_sql(), f"'{_TODAY.isoformat()}'")
              .replace("CURRENT_DATE()", f"'{_TODAY.isoformat()}'")
              .replace("DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE", "ACTION_QUEUE"))
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE ACTION_QUEUE ({', '.join(_COLS)})")
    con.executemany(f"INSERT INTO ACTION_QUEUE VALUES ({', '.join('?' * len(_COLS))})",
                    [tuple(r[c] for c in _COLS) for r in rows])
    cur = con.execute(sqlglot.transpile(sql, read="snowflake", write="sqlite")[0])
    return pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])


@pytest.fixture(autouse=True)
def _account_today(monkeypatch):
    monkeypatch.setattr(logic_wb, "account_today", lambda: _TODAY)


@pytest.mark.parametrize(("include_closed", "extra_open", "matching"), [(True, 0, 610), (False, 300, 610)])
def test_kpis_are_the_uncapped_totals_and_match_action_summary_over_everything(include_closed, extra_open,
                                                                                matching):
    """include_closed: 300 closed CRITICAL rows compete for the 500 slots; toggle off: 610 open items."""
    rows = _queue(extra_open)
    frame = _execute(workbench_sql.action_center("ALL", include_closed, 500, with_kpi_totals=True), rows)
    assert len(frame) == 500                                                      # the read IS capped
    truth = logic_wb.action_summary(pd.DataFrame(rows))                            # every row, no window cols
    assert truth == {"open": 306.0 + extra_open, "critical_high": 5.0, "overdue": 5.0, "unassigned": 300.0,
                     "estimated_usd": 15300.0 + 10.0 * extra_open, "deferred": 4.0}
    assert logic_wb.action_summary(frame) == truth
    assert int(frame.iloc[0]["KPI_MATCHING_TOTAL"]) == matching
    capped = logic_wb.action_summary(frame.drop(columns=list(logic_wb.ACTION_WINDOW_COLS)))
    if include_closed:
        assert capped == truth          # open work sorts first: closed history no longer evicts any of it
    else:
        # >500 open items: a pandas sum over the capped frame (the pre-fix KPI) undercounts
        assert capped["open"] < truth["open"] and capped["estimated_usd"] < truth["estimated_usd"]


def test_include_closed_sorts_open_work_first_so_closed_history_never_evicts_it():
    frame = _execute(workbench_sql.action_center("ALL", True, 500, with_kpi_totals=True), _queue())
    status = frame["STATUS"].str.upper()
    assert int(status.isin(("OPEN", "IN_PROGRESS")).sum()) == 310                 # every open item is listed
    first_closed = int((~status.isin(("OPEN", "IN_PROGRESS"))).idxmax())
    assert status.iloc[:first_closed].isin(("OPEN", "IN_PROGRESS")).all()


def test_builder_shape_and_default_unchanged():
    sql = workbench_sql.action_center("ALL", True, 500, with_kpi_totals=True)
    sqlglot.parse_one(sql, read="snowflake")
    assert sql.index("KPI_OPEN_TOTAL") < sql.index("LIMIT 500")
    assert account_today_sql() in sql                                              # Central, not session TZ
    # the Proof read and the default stay as they were (test_company_filter_scoping locks "OVER ()" absent)
    assert "KPI_" not in workbench_sql.action_center("ALL", False, 500, with_totals=True)
    assert "IFF(UPPER(STATUS) IN ('OPEN', 'IN_PROGRESS'), 0, 1)" not in workbench_sql.action_center("ALL", False)


# ---------------------------------------------------------------------------------------- the page ----

def _render(monkeypatch, frame: pd.DataFrame | None = None, *, mine: bool = False, rows: list[dict] | None = None,
            sqls: list[str] | None = None):
    """Render Action Center over ``frame`` (the toggle-off read), or -- with ``rows`` -- execute whatever SQL the
    page builds against that ACTION_QUEUE (so the 'Assigned to me' read is the page's own)."""
    from tests.test_probe_absence_split import _ok
    from tests.test_workbench_failed_reads import _patch_page
    wb, fake, seen = _patch_page(monkeypatch, {"action_center_ALL_True": _ok(frame if frame is not None
                                                                              else pd.DataFrame())})
    if rows is not None:
        def _run(sql, *_a, key: str = "", **_k):
            if sqls is not None:
                sqls.append(sql)
            return _ok(_execute(sql, rows)) if key.startswith("action_center_") else _ok(pd.DataFrame())
        monkeypatch.setattr(wb, "run", _run)
    monkeypatch.setattr(wb, "account_today", lambda: _TODAY)
    monkeypatch.setattr(wb, "viewer_name", lambda: "ANA" if mine else "")
    monkeypatch.setattr(wb, "_with_held", lambda f, **_k: f)
    monkeypatch.setattr(wb, "is_operator", lambda: False)                         # no create-item form
    if not mine:
        fake._off = ("action_assigned_to_me",)
    wb.render_action_center("ALL")
    (kpis,) = seen["kpis"]
    return {k["label"]: k["value"] for k in kpis}, fake.text("caption")


def test_page_reads_the_window_totals_and_discloses_the_cap(monkeypatch):
    frame = _execute(workbench_sql.action_center("ALL", True, 500, with_kpi_totals=True), _queue())
    kpis, captions = _render(monkeypatch, frame)
    assert kpis["Open work"] == "306" and kpis["Unassigned"] == "300"
    assert kpis["Estimated opportunity"] == "$15,300"
    assert "The list shows the first 500 of 610 matching items" in captions
    assert "the counts above cover all of them." in captions


def test_assigned_to_me_counts_the_rows_it_shows(monkeypatch):
    sqls: list[str] = []
    kpis, _captions = _render(monkeypatch, mine=True, rows=_queue(), sqls=sqls)
    assert kpis["Open work"] == "6"         # ANA's 5 overdue HIGH items + the one that resumed today, not 306
    assert kpis["Unassigned"] == "0" and kpis["Critical / high"] == "5"
    assert "UPPER(TRIM(COALESCE(OWNER, ''))) = 'ANA'" in sqls[0]       # filtered in the read, before the LIMIT


def test_assigned_to_me_lists_the_viewers_work_past_the_cap(monkeypatch):
    """600 other-owner HIGH items fill the 500-row cap ahead of the viewer's LOW items: a row filter over the
    capped read listed none of them and counted 0; the owner-scoped read lists and counts all of them."""
    rows = [_row(i, SEVERITY="HIGH", OWNER="BO") for i in range(600)]
    rows += [_row(900 + i, SEVERITY="LOW", OWNER="ana ", ESTIMATED_USD=20.0) for i in range(7)]
    kpis, captions = _render(monkeypatch, mine=True, rows=rows)
    assert kpis["Open work"] == "7" and kpis["Estimated opportunity"] == "$140.00"
    assert "The list shows the first" not in captions                   # 7 rows: nothing is capped


# ------------------------------------------- 'Assigned to me' + the open item (v4.608 review fix) ----

def _render_mine(monkeypatch, rows: list[dict], *, selected: str = "", deep_link: str = "",
                 fail_item: bool = False, real_held: bool = False):
    """Render Action Center with both toggles on for viewer ANA, executing every ACTION_QUEUE read the page builds
    against ``rows``; ``selected`` is the master-detail selection. Returns (kpis, listed ids, [(key, sql)], seen)."""
    from tests.test_probe_absence_split import _failed, _ok
    from tests.test_workbench_failed_reads import _patch_page
    wb, fake, seen = _patch_page(monkeypatch, {})
    reads: list[tuple[str, str]] = []

    def _run(sql, *_a, key: str = "", **_k):
        reads.append((key, sql))
        if fail_item and key.startswith("action_center_item_"):
            return _failed("timeout")
        return _ok(_execute(sql, rows)) if key.startswith("action_center_") else _ok(pd.DataFrame())

    listed: list[pd.DataFrame] = []
    monkeypatch.setattr(wb, "run", _run)
    monkeypatch.setattr(wb, "master_detail", lambda display, *_a, **_k: listed.append(display))
    monkeypatch.setattr(wb, "account_today", lambda: _TODAY)
    monkeypatch.setattr(wb, "viewer_name", lambda: "ANA")
    monkeypatch.setattr(wb, "is_operator", lambda: False)
    monkeypatch.setattr(wb, "navigation_context", lambda: {"action_id": deep_link} if deep_link else {})
    if not real_held:
        monkeypatch.setattr(wb, "_with_held", lambda f, **_k: f)
    fake.session_state["_ow_md_sel_action_center"] = selected
    wb.render_action_center("ALL")
    kpis = {k["label"]: k["value"] for k in seen["kpis"][0]} if seen["kpis"] else {}
    ids = list(listed[0]["ACTION_ID"].astype(str)) if listed else []
    return kpis, ids, reads, seen


def _queue_sql(reads: list[tuple[str, str]]) -> str:
    (sql,) = [s for k, s in reads if k.startswith("action_center_ALL_")]
    return sql


def test_assigned_to_me_read_never_depends_on_the_selected_item(monkeypatch):
    """Review fix: the open item was baked into the owner-scoped read's SQL (OR ACTION_ID = '<pin>'), and run()
    caches by SQL text, so every newly clicked item missed the cache and re-read ACTION_QUEUE live."""
    rows = [_row(1, OWNER="ANA"), _row(2, OWNER="ana", SEVERITY="HIGH"), _row(3, OWNER="BO")]
    _k1, ids1, reads1, _s1 = _render_mine(monkeypatch, rows, selected="a0001")
    _k2, ids2, reads2, _s2 = _render_mine(monkeypatch, rows, selected="a0002")
    assert _queue_sql(reads1) == _queue_sql(reads2)                   # one cache identity for every selection
    assert [k for k, _ in reads1] == [k for k, _ in reads2]
    assert "UPPER(TRIM(COALESCE(OWNER, ''))) = 'ANA'" in _queue_sql(reads1)      # still filtered in the read
    assert "a0001" not in _queue_sql(reads1) and "a0002" not in _queue_sql(reads2)
    assert not [k for k, _ in reads1 + reads2 if k.startswith("action_center_item_")]   # own items: no extra read
    assert sorted(ids1) == sorted(ids2) == ["a0001", "a0002"]


def test_the_open_item_someone_else_owns_stays_listed_outside_the_viewers_counts(monkeypatch):
    rows = [_row(1, OWNER="ANA"), _row(2, OWNER="BO", SEVERITY="CRITICAL")]
    kpis, ids, reads, _seen = _render_mine(monkeypatch, rows, selected="a0002")
    assert sorted(ids) == ["a0001", "a0002"]                          # BO's open item stays on screen
    assert kpis["Open work"] == "1" and kpis["Critical / high"] == "0"  # ...outside ANA's counts
    assert "a0002" not in _queue_sql(reads)
    ((key, sql),) = [(k, s) for k, s in reads if k.startswith("action_center_item_")]
    assert "a0002" in key and "ACTION_ID = 'a0002'" in sql and "KPI_" not in sql
    sqlglot.parse_one(sql, read="snowflake")


def test_a_deep_link_to_someone_elses_item_lists_it_with_zero_counts_when_the_viewer_owns_nothing(monkeypatch):
    rows = [_row(2, OWNER="BO", SEVERITY="CRITICAL", ESTIMATED_USD=900.0)]
    kpis, ids, _reads, seen = _render_mine(monkeypatch, rows, deep_link="a0002")
    assert ids == ["a0002"]
    assert kpis["Open work"] == "0" and kpis["Critical / high"] == "0"
    assert kpis["Estimated opportunity"] == "$0.00"
    assert not [s for s, _m in seen["empty"] if s == "clean"]         # the item is shown, not "nothing assigned"


def test_a_failed_open_item_read_says_so_and_keeps_the_viewers_list(monkeypatch):
    rows = [_row(1, OWNER="ANA"), _row(2, OWNER="BO")]
    kpis, ids, _reads, seen = _render_mine(monkeypatch, rows, selected="a0002", fail_item=True)
    assert ids == ["a0001"] and kpis["Open work"] == "1"
    assert ("unavailable", "The open work item could not be read, so it is not listed here.") in seen["empty"]
    assert "boom (timeout)" in seen["detail"]


def test_worst_case_summary_reads_stay_within_the_declared_contract(monkeypatch):
    """Include completed work + 'Assigned to me' + someone else's open item: the queue, the one-row item read and
    Held? -- the read-model caption ('up to N reads') must not under-declare it."""
    from app.logic.read_models import get_contract
    rows = [_row(1, OWNER="ANA", STATUS="DONE", COMPLETED_AT="2026-09-25 10:00:00",
                 SOURCE_ENTITY_TYPE="WAREHOUSE", SOURCE_ENTITY_KEY="WH_A"),
            _row(2, OWNER="BO")]
    _kpis, ids, reads, _seen = _render_mine(monkeypatch, rows, selected="a0002", real_held=True)
    keys = [k for k, _ in reads]
    assert sorted(ids) == ["a0001", "a0002"] and "action_held_signals" in keys
    assert len(keys) == 3 <= get_contract("action_center").summary_reads


def test_the_item_read_and_the_default_builder():
    rows = [_row(1, OWNER="ANA"), _row(2, OWNER="BO", SEVERITY="CRITICAL")]
    item = _execute(workbench_sql.action_center("ALL", True, 1, action_id=" a0002 "), rows)
    assert list(item["ACTION_ID"]) == ["a0002"] and not [c for c in item.columns if c.startswith("KPI_")]
    # without an owner or an item, the builder (and its cache identity) is byte-identical to before
    assert workbench_sql.action_center("ALL", True, 500, with_kpi_totals=True) == workbench_sql.action_center(
        "ALL", True, 500, with_kpi_totals=True, owner="", action_id="")
