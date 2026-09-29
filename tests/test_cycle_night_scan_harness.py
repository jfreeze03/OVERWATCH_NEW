"""Next-Fifty #36: the whole-night roll-up SQL EXECUTED in sqlite (the hottest shared read).

``etl_control_sql.cycle_night_health_scan`` feeds the Brief, the Control Room and Operations ▸ Tonight
through one probe read, so a wrong column here silently blanks the whole-night signals on every morning
surface. The real builder output runs below against a seeded CONTROL_STATUS (15 nights: a failed night, a
long clean night, a retried task, tonight in flight, a terminal workflow and a workflow that usually ends
after it), with the Snowflake builtins it uses registered as Python functions and the pace CTE's QUALIFY
rewritten to a filtered subquery (the rewrite asserts its fragment exists, so a changed builder fails loudly
instead of silently testing less). It proves:

- the usual END offset uses clean prior finishes only (a failed night's crash-short end is excluded), and
  END_NIGHTS_COUNT counts them;
- tonight's START / END offsets, and the pace marker = the clean-finished workflow that usually ends latest
  BEFORE the terminal (never the terminal, never one below NIGHT_END_MIN_NIGHTS clean finishes);
- PACE_* is identical on every row and the ETA columns change NOTHING else: every pre-#36 column (the
  statuses and the uncapped TOTAL_* roll-ups) equals the fallback (eta_columns=False) SQL's row for row;
- the failure path: when the enriched SQL fails, attention.cycle_night_read serves the pre-#36 roll-up, so
  the Brief / Control Room whole-night summary is unchanged (never blank).
Pure: no Snowflake, runs on the floor-compat leg."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.data import etl_control_sql as etl
from app.logic.insights import cycle_night_summary, cycle_timeline_frame

_NOW = "2026-09-16 02:00:00"          # tonight's cycle (night of 2026-09-15) is four hours in
_CTRL = "ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS"
_FMT = "%Y-%m-%d %H:%M:%S"


def _parse(value) -> tuple[datetime, bool]:
    text = str(value)
    if len(text) == 10:
        return datetime.strptime(text, "%Y-%m-%d"), True
    return datetime.strptime(text[:19], _FMT), False


def _dateadd(unit, n, value):
    """Snowflake DATEADD: a DATE plus days stays a DATE; anything else is a timestamp."""
    if value is None:
        return None
    ts, is_date = _parse(value)
    out = ts + timedelta(**{f"{str(unit).lower()}s": int(n)})
    return out.strftime("%Y-%m-%d") if is_date and str(unit).lower() == "day" else out.strftime(_FMT)


def _datediff(unit, a, b):
    if a is None or b is None:
        return None
    (ta, _), (tb, _) = _parse(a), _parse(b)
    if str(unit).lower() == "second":
        return int((tb - ta).total_seconds())
    if str(unit).lower() == "day":
        return (tb.date() - ta.date()).days
    raise ValueError(unit)


class _MaxBy:
    def __init__(self):
        self.key, self.value = None, None

    def step(self, value, key):
        if key is not None and (self.key is None or key > self.key):
            self.key, self.value = key, value

    def finalize(self):
        return self.value


class _Median:
    """Snowflake MEDIAN: NULLs ignored, the mean of the two middles on an even count."""

    def __init__(self):
        self.values: list[float] = []

    def step(self, value):
        if value is not None:
            self.values.append(float(value))

    def finalize(self):
        if not self.values:
            return None
        s, n = sorted(self.values), len(self.values)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _to_sqlite(sql: str) -> str:
    sql = sql.replace(_CTRL, "CONTROL_STATUS")
    assert "CURRENT_TIMESTAMP()" in sql
    sql = sql.replace("CURRENT_TIMESTAMP()", f"'{_NOW}'")
    if "pace AS (" in sql:
        m = re.search(r"pace AS \(\n  SELECT (?P<cols>.*?)\n(?P<rest>  FROM graded g\n.*?)"
                      r"  QUALIFY (?P<win>ROW_NUMBER\(\) OVER \([^\n]*\)) = 1\n\)", sql, re.S)
        assert m, "the pace CTE's QUALIFY changed shape"
        sql = sql.replace(m.group(0), f"pace AS (\n  SELECT * FROM (SELECT {m.group('cols')}, "
                                      f"{m.group('win')} AS _RN\n{m.group('rest')}  ) WHERE _RN = 1\n)")
    assert "QUALIFY" not in sql and "::" not in sql and "CURRENT_TIMESTAMP" not in sql
    return sql


def _t(day: str, hhmm: str, plus_days: int = 0) -> str:
    return (datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M") + timedelta(days=plus_days)).strftime(_FMT)


_PRIOR = [f"2026-09-{d:02d}" for d in range(1, 15)]     # 14 prior nights
_TONIGHT = "2026-09-15"
# WF_MID's clean prior ends (next morning): six at 00:20, six at 00:40 and one long 03:00 night, so the
# clean median is 00:40 (9600s from the 22:00 start) — counting the FAILED night's crash-short 23:00 end
# would drag it to 9000s.
_MID_END = {d: ("00:20" if i % 2 == 0 else "00:40") for i, d in enumerate(_PRIOR)}
_MID_END["2026-09-10"] = "03:00"                         # a long clean night (a spike), still a clean finish


def _rows() -> list[tuple]:
    rows: list[tuple] = []

    def add(wf, task, status, day, start, end, *, start_next=0, end_next=1):
        rows.append((wf, task, status, _t(day, start, start_next), None if end is None else _t(day, end, end_next)))

    for d in _PRIOR:
        add("WF_START", "T_START", "SUCCEEDED", d, "22:00", "22:20", end_next=0)
        if d == "2026-09-05":
            add("WF_MID", "T_MID", "FAILED", d, "22:30", "23:00", end_next=0)       # crash-short, excluded
        elif d == "2026-09-10":
            add("WF_MID", "T_MID", "SUCCEEDED", d, "22:30", "03:00")
        elif d == "2026-09-03":                                                       # a retry collapses
            add("WF_MID", "T_MID", "FAILED", d, "22:30", "22:35", end_next=0)
            add("WF_MID", "T_MID", "SUCCEEDED", d, "22:36", "00:20")
        else:
            add("WF_MID", "T_MID", "SUCCEEDED", d, "22:30", _MID_END[d])
        add("WF_LATE", "T_LATE", "SUCCEEDED", d, "01:00", "03:30", start_next=1)
        add("WF_END", "T_END", "SUCCEEDED", d, "04:00", "05:00", start_next=1)
        add("WF_ODD", "T_ODD", "SUCCEEDED", d, "22:05", "05:30")                      # usually ends AFTER the terminal
        add("WF_MISS", "T_MISS", "SUCCEEDED", d, "23:00", "23:30", end_next=0)
        add("WF_FAILT", "T_FAILT", "SUCCEEDED", d, "22:40", "23:40", end_next=0)
        if d >= "2026-09-12":                                                         # only 3 clean nights
            add("WF_NEW", "T_NEW", "SUCCEEDED", d, "22:10", "01:50")
    t = _TONIGHT
    add("WF_START", "T_START", "SUCCEEDED", t, "22:00", "22:20", end_next=0)
    add("WF_MID", "T_MID", "SUCCEEDED", t, "22:30", "01:03")                          # 23 min past its usual
    add("WF_LATE", "T_LATE", "SUCCEEDED", t, "01:10", "01:40", start_next=1, end_next=1)
    add("WF_LATE", "T_LATE2", "RUNNING", t, "01:40", None, start_next=1)             # still running
    add("WF_ODD", "T_ODD", "SUCCEEDED", t, "22:05", "00:00")
    add("WF_NEW", "T_NEW", "SUCCEEDED", t, "22:10", "01:50")
    add("WF_FAILT", "T_FAILT", "FAILED", t, "22:40", "22:50", end_next=0)
    # WF_END: not started yet (PENDING); WF_MISS: absent past its usual start + grace (MISSING)
    return rows


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("DATEDIFF", 3, _datediff)
    con.create_aggregate("MAX_BY", 2, _MaxBy)
    con.create_aggregate("MEDIAN", 1, _Median)
    con.execute("CREATE TABLE CONTROL_STATUS (WORKFLOW_NAME TEXT, TASK_NAME TEXT, TASK_STATUS TEXT, "
                "TASK_START_DTTM TEXT, TASK_END_DTTM TEXT)")
    con.executemany("INSERT INTO CONTROL_STATUS VALUES (?, ?, ?, ?, ?)", _rows())
    return con


def _scan(con: sqlite3.Connection, **kw) -> pd.DataFrame:
    sql = etl.cycle_night_health_scan(_CTRL, start_workflow="WF_START", **kw)
    return pd.read_sql_query(_to_sqlite(sql), con)


@pytest.fixture(scope="module")
def frames():
    con = _db()
    return {"end": _scan(con, end_workflow="WF_END"), "noend": _scan(con),
            "base": _scan(con, end_workflow="WF_END", eta_columns=False)}


def _row(df: pd.DataFrame, wf: str) -> pd.Series:
    return df[df["WORKFLOW_NAME"] == wf].iloc[0]


def test_statuses_match_a_hand_grade(frames):
    df = frames["end"]
    got = dict(zip(df["WORKFLOW_NAME"], df["NIGHT_STATUS"], strict=True))
    assert got == {"WF_START": "OK", "WF_MID": "OK", "WF_LATE": "RUNNING", "WF_END": "PENDING",
                   "WF_ODD": "OK", "WF_NEW": "OK", "WF_MISS": "MISSING", "WF_FAILT": "FAILED"}
    assert str(df["CYCLE_DATE"].iloc[0]) == _TONIGHT


def test_usual_end_counts_clean_prior_finishes_only(frames):
    mid = _row(frames["end"], "WF_MID")
    assert mid["TYPICAL_END_OFFSET_SEC"] == 9600            # the failed night's 3600 would make it 9000
    assert mid["END_NIGHTS_COUNT"] == 13                     # 14 nights ran, one failed
    assert mid["NIGHTS_RAN_COUNT"] == 14
    assert mid["TYPICAL_OFFSET_SEC"] == 1800                 # the start offset is unchanged (all nights)
    assert _row(frames["end"], "WF_NEW")["END_NIGHTS_COUNT"] == 3
    assert _row(frames["end"], "WF_END")["TYPICAL_END_OFFSET_SEC"] == 25200
    assert _row(frames["end"], "WF_END")["END_NIGHTS_COUNT"] == 14


def test_tonights_offsets(frames):
    df = frames["end"]
    mid, late, end = _row(df, "WF_MID"), _row(df, "WF_LATE"), _row(df, "WF_END")
    assert (mid["START_OFFSET_SEC"], mid["END_OFFSET_SEC"]) == (1800, 10980)
    assert late["START_OFFSET_SEC"] == 11400 and pd.isna(late["END_OFFSET_SEC"])     # running: no end
    assert pd.isna(end["START_OFFSET_SEC"]) and pd.isna(end["END_OFFSET_SEC"])       # not started
    assert pd.isna(_row(df, "WF_FAILT")["END_OFFSET_SEC"])                          # failed: no clean end


def test_pace_marker_is_the_latest_usual_end_before_the_terminal(frames):
    df = frames["end"]
    # WF_ODD (usual end after the terminal) and WF_NEW (3 clean nights) are passed over for WF_MID
    assert set(df["PACE_WORKFLOW_NAME"]) == {"WF_MID"}
    assert set(df["PACE_LATE_SEC"]) == {10980 - 9600}
    assert set(df["PACE_END_AT"]) == {"2026-09-16 01:03:00"}
    # with no terminal configured there is no upper bound, so the latest usual end wins
    assert set(frames["noend"]["PACE_WORKFLOW_NAME"]) == {"WF_ODD"}
    assert set(frames["noend"]["PACE_LATE_SEC"]) == {7200 - 27000}


def test_eta_columns_change_nothing_else(frames):
    base, enriched = frames["base"], frames["end"]
    assert list(enriched.columns[:len(base.columns)]) == list(base.columns)
    pd.testing.assert_frame_equal(enriched[list(base.columns)], base)       # same rows, same order, same totals
    assert enriched["TOTAL_WORKFLOWS"].iloc[0] == len(enriched) == 8           # no row multiplication
    assert enriched["TOTAL_FAILED_TASKS"].iloc[0] == enriched["FAILED_TASK_COUNT"].sum() == 1
    for col, state in (("TOTAL_FAILED_WF", "FAILED"), ("TOTAL_MISSING_WF", "MISSING"),
                       ("TOTAL_RUNNING_WF", "RUNNING"), ("TOTAL_PENDING_WF", "PENDING")):
        assert enriched[col].iloc[0] == (enriched["NIGHT_STATUS"] == state).sum() == 1


def test_timeline_over_the_executed_frame(frames):
    tl = cycle_timeline_frame(frames["end"], start_workflow="WF_START", end_workflow="WF_END")
    notes = dict(zip(tl["WORKFLOW_NAME"], tl["TIMELINE_NOTE"], strict=True))
    assert notes["WF_START"] == "Starts the cycle" and notes["WF_END"] == "Finishes the cycle"
    assert notes["WF_MID"] == "Pace marker" and notes["WF_NEW"] == "Finished last so far"
    assert tl["WORKFLOW_NAME"].iloc[0] == "WF_START" and tl["WORKFLOW_NAME"].iloc[-1] == "WF_END"
    assert pd.isna(tl.set_index("WORKFLOW_NAME").loc["WF_NEW", "USUAL_END_OFFSET_SEC"])   # < 4 clean nights


# --- the failure path: an error in the additive columns never blanks the roll-up ---------------------

def _executor(con: sqlite3.Connection, calls: list[str], *, fail_enriched: str = ""):
    def _run(sql, **kwargs):
        calls.append(kwargs.get("key", ""))
        if fail_enriched and "pace AS (" in sql:
            return QueryResult(df=pd.DataFrame(), ok=False, error="invalid identifier 'END_NIGHTS_COUNT'",
                               error_kind=fail_enriched, source=kwargs.get("source", ""))
        return QueryResult(df=pd.read_sql_query(_to_sqlite(sql), con), ok=True, source=kwargs.get("source", ""))
    return _run


_SETTINGS = {"ETL_CONTROL_STATUS_FQN": _CTRL, "ETL_CYCLE_START_WORKFLOW": "WF_START",
             "ETL_CYCLE_END_WORKFLOW": "WF_END"}


def test_failed_eta_columns_fall_back_to_the_base_roll_up(monkeypatch):
    from app.ui import attention
    con = _db()
    logged: list[str] = []
    monkeypatch.setattr(attention, "record_error", lambda page, exc, context="": logged.append(str(exc)))
    monkeypatch.setattr(attention, "_night_fallback_logged", set())
    healthy_calls: list[str] = []
    monkeypatch.setattr(attention, "run", _executor(con, healthy_calls))
    healthy = attention.cycle_night_read(_SETTINGS, page="Brief")
    assert healthy_calls == ["attn_cycle_night"] and "PACE_LATE_SEC" in healthy.df.columns
    for kind in ("missing_column", "unknown_function", "other"):
        calls: list[str] = []
        monkeypatch.setattr(attention, "run", _executor(con, calls, fail_enriched=kind))
        res = attention.cycle_night_read(_SETTINGS, page="Brief")
        assert calls == ["attn_cycle_night", "attn_cycle_night_base"], kind
        assert res.ok and "PACE_LATE_SEC" not in res.df.columns
        # the whole-night summary the Brief / Control Room verdicts read is exactly the healthy one
        assert cycle_night_summary(res.df) == cycle_night_summary(healthy.df), kind
    assert len(logged) == 3 and all("served the base roll-up" in m for m in logged)
    attention.cycle_night_read(_SETTINGS, page="Control Room")               # the same fault again ...
    assert len(logged) == 3                                                  # ... is logged once per process
    # the Operations timeline hides itself on the fallback frame (no offset columns); never raises
    assert cycle_timeline_frame(res.df).empty


def test_absent_or_timeout_never_pays_a_second_read(monkeypatch):
    from app.ui import attention
    con = _db()
    for kind in ("absent", "timeout"):
        calls: list[str] = []
        monkeypatch.setattr(attention, "run", _executor(con, calls, fail_enriched=kind))
        res = attention.cycle_night_read(_SETTINGS, page="Brief")
        assert calls == ["attn_cycle_night"] and not res.ok, kind


def test_fallback_that_also_fails_returns_the_original_error(monkeypatch):
    from app.ui import attention
    calls: list[str] = []

    def _all_fail(sql, **kwargs):
        calls.append(kwargs.get("key", ""))
        return QueryResult(df=pd.DataFrame(), ok=False, error=f"boom {len(calls)}", error_kind="other")

    monkeypatch.setattr(attention, "run", _all_fail)
    res = attention.cycle_night_read(_SETTINGS, page="Brief")
    assert calls == ["attn_cycle_night", "attn_cycle_night_base"]
    assert not res.ok and res.error == "boom 1"
    etl_view = attention.etl_attention(dict(_SETTINGS, ETL_CYCLE_END_WORKFLOW=""), page="Brief")
    assert etl_view["night"] == {} and etl_view["eta"] == {}
