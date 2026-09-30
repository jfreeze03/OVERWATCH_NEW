"""Next-Fifty #38 remainder, v4.604.0 review r1: the cluster-cap gate's RENDER paths without AppTest.

tests/test_cluster_cap_shaped.py renders the real page, but AppTest is skipped on the floor CI leg
(streamlit 1.52, _APPTEST_BUTTONGROUP_OK). These tests call the page's own _cluster_cap_check with a fake
``st`` and a recording ``run`` instead, so every leg runs them:

  * the toggle OFF reads nothing and says the cap was not checked;
  * R1-25 a FAILED read returns the frame unjudged: guard() shows the red line once, the failed caption
    says the cap was not checked, and the histogram is never summarised (a disabled ``if not res.ok``
    used to survive every floor test and judged the failure as "No queries" everywhere);
  * R1-10 an EMPTY SHOW WAREHOUSES (or one listing none of the profile's warehouses) says the cluster
    ranges are UNKNOWN (needs_setup: zero rows are never red) instead of "no warehouse ... has
    MAX_CLUSTER_COUNT above 1", offers no toggle and reads nothing; a partly-listed profile names its
    unknown warehouses;
  * R1-7 the read starts at MIDNIGHT (account time) N days back, so a Last-month window's first day is
    inside in full;
  * R1-8 the cluster-use table's hour columns say they count the hour a query STARTED in."""

from __future__ import annotations

import contextlib
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.logic.insights import with_warehouse_settings

pytest.importorskip("streamlit")

_FAILED_CAPTION = ("The cluster-cap check failed, so the cap was not checked: add-a-cluster advice below suggests "
                   "no higher MAX_CLUSTER_COUNT for a multi-cluster warehouse.")
_NO_CAP_CAPTION = ("No warehouse in this profile has MAX_CLUSTER_COUNT above 1 in SHOW WAREHOUSES, so there is "
                   "no cluster cap to check.")
_TODAY = date(2026, 9, 30)


class _FakeSt:
    """The slice of streamlit _cluster_cap_check touches, recording what a viewer would see."""

    def __init__(self, toggle_on: bool = True) -> None:
        self.captions: list[str] = []
        self.toggles: list[tuple[str, str, str]] = []
        self.expanders: list[str] = []
        self._on = toggle_on
        self.column_config = SimpleNamespace(
            NumberColumn=lambda label=None, **k: {"label": label, **k},
            TextColumn=lambda label=None, **k: {"label": label, **k})

    def caption(self, text, *_a, **_k) -> None:
        self.captions.append(str(text))

    def toggle(self, label, *_a, key: str = "", help: str = "", **_k) -> bool:
        self.toggles.append((key, str(label), str(help)))
        return self._on

    def expander(self, label, *_a, **_k):
        self.expanders.append(str(label))
        return contextlib.nullcontext()

    def text(self) -> str:
        return " ".join(self.captions)


def _patch(monkeypatch, *, toggle_on: bool = True, read: QueryResult | None = None) -> tuple[_FakeSt, dict]:
    import app.ui.pages.cost_parts.optimize as opt

    fake = _FakeSt(toggle_on)
    seen: dict = {"runs": [], "guard": [], "empty": [], "tables": []}

    def fake_run(sql, *_a, **kw):
        seen["runs"].append((str(sql), kw))
        return read if read is not None else QueryResult(df=pd.DataFrame(), ok=True, source="t")

    def fake_guard(res, msg, *_a, **_k):
        seen["guard"].append((res, msg))
        return bool(res.ok)

    def fake_table(df, *_a, column_config=None, **_k):
        seen["tables"].append((df, column_config or {}))

    monkeypatch.setattr(opt, "st", fake)
    monkeypatch.setattr(opt, "run", fake_run)
    monkeypatch.setattr(opt, "guard", fake_guard)
    monkeypatch.setattr(opt, "empty_state", lambda kind, msg, *_a, **_k: seen["empty"].append((kind, msg)))
    monkeypatch.setattr(opt, "styled_table", fake_table)
    monkeypatch.setattr(opt, "result_caption", lambda *_a, **_k: None)
    monkeypatch.setattr(opt, "toggle_cost_hint", lambda _key: "HINT.")
    monkeypatch.setattr(opt, "account_today", lambda: _TODAY)
    return fake, seen


def _profile(*names: str) -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": n, "COMPANY": "ALFA", "AUTO_SUSPEND": 300} for n in names])


def _show(rows: dict[str, int]) -> pd.DataFrame:
    return pd.DataFrame([{"name": n, "size": "X-Small", "min_cluster_count": 1, "max_cluster_count": m,
                          "scaling_policy": "STANDARD", "auto_suspend": 300} for n, m in rows.items()])


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="t")


def _check(frame: pd.DataFrame, show: pd.DataFrame, bounds: tuple | None = None, days: int = 30) -> pd.DataFrame:
    from app.ui.pages.cost_parts.optimize import _cluster_cap_check
    return _cluster_cap_check(frame, _ok(show), days, "ALL", bounds)


# ---------------------------------------------------------------------------
# R1-25: a failed read is never judged
# ---------------------------------------------------------------------------

def test_a_failed_read_returns_the_frame_unjudged(monkeypatch):
    import app.ui.pages.cost_parts.optimize as opt

    failed = QueryResult(df=pd.DataFrame(), ok=False, source="t", error_kind="timeout",
                         error="Statement reached its statement or warehouse timeout of 180 second(s)")
    fake, seen = _patch(monkeypatch, read=failed)

    def _never(*_a, **_k):
        raise AssertionError("a failed read must never be summarised or carried onto the profile")

    monkeypatch.setattr(opt, "cluster_use_summary", _never)
    monkeypatch.setattr(opt, "with_cluster_use", _never)
    show = _show({"WH_SAT": 4, "WH_LOW": 4})
    frame = with_warehouse_settings(_profile("WH_SAT", "WH_LOW"), show)
    out = _check(frame, show)
    assert out is frame                                            # unchanged: every gated row says "not checked"
    assert len(seen["runs"]) == 1 and "CLUSTER_NUMBER" in seen["runs"][0][0]
    assert [g[0] for g in seen["guard"]] == [failed]               # the red "Query failed" line, once
    assert _FAILED_CAPTION in fake.captions
    assert not seen["tables"] and not fake.expanders               # no cluster-use table on a failure


# ---------------------------------------------------------------------------
# R1-10: an empty SHOW WAREHOUSES is "ranges unknown", never "no multi-cluster warehouse"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("show", [pd.DataFrame(), _show({"WH_ELSEWHERE": 4})], ids=["empty", "no-match"])
def test_an_unlisted_profile_reads_ranges_unknown(monkeypatch, show):
    fake, seen = _patch(monkeypatch)
    frame = with_warehouse_settings(_profile("WH_SAT", "WH_LOW"), show)
    out = _check(frame, show)
    assert out is frame
    assert len(seen["empty"]) == 1
    kind, msg = seen["empty"][0]
    assert kind == "needs_setup"                                    # zero rows: never red, never clean
    assert "cluster ranges are unknown and the cluster-cap check cannot run" in msg
    assert _NO_CAP_CAPTION not in fake.text()
    assert not fake.toggles and not seen["runs"]


def test_a_partly_listed_profile_names_its_unknown_warehouses(monkeypatch):
    fake, seen = _patch(monkeypatch, toggle_on=False)
    show = _show({"WH_SAT": 4})
    frame = with_warehouse_settings(_profile("WH_SAT", "WH_GONE"), show)
    _check(frame, show)
    assert not seen["empty"]
    assert ("Cluster-cap check: 1 multi-cluster warehouse(s) in this profile, over the last 35 days. 1 "
            "warehouse(s) in this profile are not in SHOW WAREHOUSES, so their cluster range is unknown and "
            "they are not checked. HINT.") in fake.captions
    assert [t[0] for t in fake.toggles] == ["sizing_cluster_check"]
    # (a) the toggle is off: nothing is read and the page says the cap was not checked
    assert not seen["runs"]
    assert ("Off: the cluster cap is not checked, so add-a-cluster advice on a multi-cluster warehouse suggests "
            "no higher MAX_CLUSTER_COUNT and prefills none.") in fake.captions
    # single-cluster only, one unlisted: the no-cap answer keeps the unknown one visible
    fake2, _seen2 = _patch(monkeypatch)
    show2 = _show({"WH_ONE": 1})
    _check(with_warehouse_settings(_profile("WH_ONE", "WH_GONE"), show2), show2)
    assert (_NO_CAP_CAPTION + " 1 warehouse(s) in this profile are not in SHOW WAREHOUSES, so their cluster "
            "range is unknown and they are not checked.") in fake2.captions
    assert not fake2.toggles


def test_a_fully_listed_single_cluster_profile_still_says_no_cap(monkeypatch):
    fake, seen = _patch(monkeypatch)
    show = _show({"WH_A": 1, "WH_B": 1})
    _check(with_warehouse_settings(_profile("WH_A", "WH_B"), show), show)
    assert fake.captions == [_NO_CAP_CAPTION]
    assert not seen["empty"] and not fake.toggles and not seen["runs"]


# ---------------------------------------------------------------------------
# R1-7 / R1-8: the read window and what an "hour" is
# ---------------------------------------------------------------------------

def test_the_read_starts_at_midnight_of_the_sizing_windows_first_day(monkeypatch):
    hist = pd.DataFrame([{"WAREHOUSE_NAME": "WH_SAT", "PEAK_CLUSTER": 4, "HOUR_COUNT": 57},
                         {"WAREHOUSE_NAME": "WH_SAT", "PEAK_CLUSTER": 1, "HOUR_COUNT": 72}])
    fake, seen = _patch(monkeypatch, read=_ok(hist))
    show = _show({"WH_SAT": 4})
    # Last month, read on Sep 30: the sizing window is Aug 1 00:00 .. Sep 1 00:00 (Central)
    out = _check(with_warehouse_settings(_profile("WH_SAT"), show), show,
                 bounds=(date(2026, 8, 1), date(2026, 9, 1)), days=31)
    sql = seen["runs"][0][0]
    # 60 days before Sep 30 is Aug 1, and the bound is that DATE (midnight, account time) — not now - 60 days
    assert "q.START_TIME >= DATEADD('day', -60, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)" in sql
    assert "CURRENT_TIMESTAMP())\n" not in sql and "-60, CURRENT_TIMESTAMP()" not in sql
    assert date.fromordinal(_TODAY.toordinal() - 60) == date(2026, 8, 1)
    assert seen["runs"][0][1]["key"] == "cluster_use_ALL_60"
    assert out.loc[0, "CLUSTER_CHECK_DAYS"] == 60 and out.loc[0, "AT_CAP_HOUR_COUNT"] == 57
    # R1-8: the cluster-use table says its hours are start hours
    (_df, cfg), = seen["tables"]
    assert "started on a cluster of this warehouse" in cfg["ACTIVE_HOUR_COUNT"]["help"]
    assert "only in the hour it started" in cfg["ACTIVE_HOUR_COUNT"]["help"]
    assert "ran on a cluster" not in cfg["ACTIVE_HOUR_COUNT"]["help"]
    assert "a query started on the current MAX_CLUSTER_COUNT cluster" in cfg["AT_CAP_HOUR_COUNT"]["help"]
    assert "counted by the hour a query STARTED in" in fake.text()          # the note under the table
    (_key, _label, toggle_help), = fake.toggles
    assert "the highest cluster any query started on in each clock hour" in toggle_help
