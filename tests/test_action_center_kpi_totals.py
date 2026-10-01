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
  the viewer's work.

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


def test_the_open_item_stays_listed_but_never_counts_as_the_viewers_work(monkeypatch):
    rows = [_row(1, OWNER="ANA"), _row(2, OWNER="BO", SEVERITY="CRITICAL")]
    sql = workbench_sql.action_center("ALL", True, 500, with_kpi_totals=True, owner="ana", keep_action_id="a0002")
    frame = _execute(sql, rows)
    assert set(frame["ACTION_ID"]) == {"a0001", "a0002"}                # BO's open item stays on screen
    assert logic_wb.action_summary(frame)["open"] == 1.0                # ...outside ANA's counts
    assert logic_wb.action_summary(frame)["critical_high"] == 0.0
    assert int(frame.iloc[0]["KPI_MATCHING_TOTAL"]) == 2
    # without the owner, the builder (and its cache identity) is byte-identical to before
    assert workbench_sql.action_center("ALL", True, 500, with_kpi_totals=True) == workbench_sql.action_center(
        "ALL", True, 500, with_kpi_totals=True, owner="", keep_action_id="a0002")
