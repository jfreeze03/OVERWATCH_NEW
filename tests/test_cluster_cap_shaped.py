"""Next-Fifty #38 remainder on the RENDERED page (AppTest, streamlit >= 1.55 like the rest of the shaped harness):
Cost ▸ Optimization & Savings ▸ Idle & sizing with the right-sizing profile loaded and two queue-heavy
multi-cluster warehouses (max 4, STANDARD) -- WH_SAT, whose queries reached cluster 4 of 4, and WH_LOW,
whose peak was cluster 3.

  (a) the "Check cluster use" toggle OFF: no CLUSTER_NUMBER read is issued and WH_SAT's add-a-cluster
      rationale says its cluster cap was not checked;
  (b) ON: WH_SAT is told to raise MAX_CLUSTER_COUNT to 5; WH_LOW reads "Size up or split (cluster cap not
      reached)";
  (c) the read FAILS: a red "Query failed" line, the failed caption, and WH_SAT reads "was not checked";
  (d) no multi-cluster warehouse in SHOW: the no-cap caption and no toggle at all.

The shared shaped harness stubs SHOW WAREHOUSES as an empty frame, so only an injected frame reaches the gate
(tests/test_prc_c1_shaped.py covers the no-multi-cluster default). The floor venv skips these
(_APPTEST_BUTTONGROUP_OK); tests/test_cluster_cap_gate.py locks the same behaviour purely and by source."""

from __future__ import annotations

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_mart_first,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult
from app.logic.sizing import RECOMMEND_BELOW_CAP, RECOMMEND_SCALE_OUT

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")

# the owner's W1c probe shapes: WH_TRXS_TRANSFORM's (57 hours at 4 of 4) and WH_ALFA_LOAD_PRD's (peak 3 of 4)
_HIST = {"WH_SAT": {1: 72, 2: 40, 3: 55, 4: 57}, "WH_LOW": {1: 300, 2: 20, 3: 3}}


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="t")


def _show(max_clusters: int) -> pd.DataFrame:
    return pd.DataFrame([{"name": n, "size": "X-Small", "min_cluster_count": 1, "max_cluster_count": max_clusters,
                          "scaling_policy": "STANDARD", "auto_suspend": 300} for n in _HIST])


def _profile() -> pd.DataFrame:
    """Two queue-heavy warehouses (60 min/day of overload queueing over 30 covered days, no spill)."""
    return pd.DataFrame([{"WAREHOUSE_NAME": n, "COMPANY": "ALFA", "CREDITS_TOTAL": 500.0, "IDLE_PCT": 5.0,
                          "QUERY_COUNT": 50000.0, "ACTIVE_QUERY_DAYS": 30.0, "P95_ELAPSED_SEC": 20.0,
                          "QUEUED_SEC": 30 * 60 * 60.0, "QUEUED_PROVISIONING_SEC": 0.0, "SPILL_REMOTE_GB": 0.0,
                          "COVERED_DAYS": 30} for n in _HIST])


def _hist() -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": n, "PEAK_CLUSTER": p, "HOUR_COUNT": c}
                         for n, h in _HIST.items() for p, c in h.items()])


def _page(monkeypatch, *, check: bool, fails: bool = False, max_clusters: int = 4,
          select: str = "") -> tuple[AppTest, list[str]]:
    import app.ui.pages.cost_parts.optimize as opt

    seen: list[str] = []
    if select:
        # An operator viewer with the right-sizing row ``select`` clicked: AppTest cannot click a grid row,
        # so the sizing table's selection event is stubbed (every other table renders unchanged).
        import app.ui.components as components
        import app.ui.pages.cost as cost

        real = components._st_dataframe

        class _Event:
            def __init__(self, rows: list[int]) -> None:
                self.selection = type("S", (), {"rows": rows})()

        def _st_dataframe(data, **kwargs):
            out = real(data, **kwargs)
            if kwargs.get("key") == "sizing_sel":
                frame = getattr(data, "data", data)
                return _Event([i for i, n in enumerate(frame["WAREHOUSE_NAME"]) if n == select])
            return out

        monkeypatch.setattr(components, "_st_dataframe", _st_dataframe)
        monkeypatch.setattr(cost, "_is_operator", lambda: True)

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        seen.append(sql)
        if sql.startswith("SHOW WAREHOUSES"):
            return _ok(_show(max_clusters))
        if "CLUSTER_NUMBER" in sql:
            if fails:
                return QueryResult(df=pd.DataFrame(), ok=False, source="t", error_kind="timeout",
                                   error="Statement reached its statement or warehouse timeout of 180 second(s)")
            return _ok(_hist())
        return _shaped_run(*args, **kwargs)

    def _mart_first(mart, live="", **kwargs):
        seen.extend([str(mart), str(live)])
        if str(kwargs.get("key") or "").startswith("sizing_"):
            return _ok(_profile())
        return _shaped_mart_first(mart, live, **kwargs)

    monkeypatch.setattr(opt, "run", _run)
    monkeypatch.setattr(opt, "run_mart_first", _mart_first)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Cost Intelligence")
    at.session_state["cost_section"] = "Optimization & Savings"
    at.session_state["opt_section"] = "Idle & sizing"
    at.session_state["sizing_load"] = True
    if check:
        at.session_state["sizing_cluster_check"] = True
    at.run()
    assert not at.exception, f"cost optimize cluster-cap check (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    return at, seen


def _sized(at) -> pd.DataFrame:
    """The right-sizing table as rendered: the dataframe carrying RECOMMENDATION + RATIONALE per warehouse."""
    for df in at.dataframe:
        v = df.value
        if isinstance(v, pd.DataFrame) and {"WAREHOUSE_NAME", "RECOMMENDATION", "RATIONALE"} <= set(v.columns):
            return v.set_index("WAREHOUSE_NAME")
    raise AssertionError("the right-sizing table did not render")


def _captions(at) -> str:
    return " ".join(str(c.value) for c in at.caption)


@_SKIP
def test_toggle_off_reads_nothing_and_says_the_cap_was_not_checked(monkeypatch):
    at, seen = _page(monkeypatch, check=False)
    assert seen, "the recorder saw no reads: the section did not render"
    assert not any("CLUSTER_NUMBER" in s for s in seen)
    sized = _sized(at)
    assert sized.loc["WH_SAT", "RECOMMENDATION"] == RECOMMEND_SCALE_OUT
    assert "was not checked" in sized.loc["WH_SAT", "RATIONALE"]
    assert "raise MAX_CLUSTER_COUNT to 5" not in sized.loc["WH_SAT", "RATIONALE"]
    caps = _captions(at)
    assert "Cluster-cap check: 2 multi-cluster warehouse(s) in this profile, over the last 35 days." in caps
    assert ("Off: the cluster cap is not checked, so add-a-cluster advice on a multi-cluster warehouse suggests "
            "no higher MAX_CLUSTER_COUNT and prefills none.") in caps
    assert "2 add-a-cluster row(s) on a multi-cluster warehouse were not checked against the cluster cap" in caps
    assert any(t.key == "sizing_cluster_check" and t.value is False for t in at.toggle)


@_SKIP
def test_toggle_on_gates_the_advice_on_hours_at_the_cap(monkeypatch):
    at, seen = _page(monkeypatch, check=True)
    reads = [s for s in seen if "CLUSTER_NUMBER" in s]
    assert reads and all("UPPER(q.WAREHOUSE_NAME) IN ('WH_LOW', 'WH_SAT')" in s for s in reads)
    assert all("DATEADD('day', -35, CURRENT_TIMESTAMP())" in s for s in reads)
    sized = _sized(at)
    assert sized.loc["WH_SAT", "RECOMMENDATION"] == RECOMMEND_SCALE_OUT
    assert ("queries reached cluster 4 of 4 in 57 hours of the last 35 days: raise MAX_CLUSTER_COUNT to 5"
            in sized.loc["WH_SAT", "RATIONALE"])
    assert sized.loc["WH_LOW", "RECOMMENDATION"] == RECOMMEND_BELOW_CAP
    assert "no query ran above cluster 3 of 4" in sized.loc["WH_LOW", "RATIONALE"]
    caps = _captions(at)
    assert ("Cluster-cap check, last 35 days: 1 of 2 multi-cluster warehouse(s) reached their current "
            "MAX_CLUSTER_COUNT in at least one hour.") in caps
    assert "1 size-up-or-split (cluster cap not reached)" in caps
    assert "were not checked against the cluster cap" not in caps
    # the cluster-use table renders the judged frame (the CSV is this frame)
    util = [df.value for df in at.dataframe
            if isinstance(df.value, pd.DataFrame) and "AT_CAP_HOUR_COUNT" in df.value.columns
            and "CLUSTER_CAP" in df.value.columns]
    assert util, "the cluster-use table did not render"
    got = util[0].set_index("WAREHOUSE_NAME")
    assert (got.loc["WH_SAT", "AT_CAP_HOUR_COUNT"], got.loc["WH_SAT", "CLUSTER_CAP"]) == (57, "Reached")
    assert (got.loc["WH_LOW", "PEAK_CLUSTERS"], got.loc["WH_LOW", "CLUSTER_CAP"]) == (3, "Not reached")


@_SKIP
def test_a_failed_read_is_red_and_the_advice_says_not_checked(monkeypatch):
    at, _seen = _page(monkeypatch, check=True, fails=True)
    errors = " ".join(str(e.value) for e in at.error)
    assert "Query failed" in errors and "timeout" in errors
    assert ("The cluster-cap check failed, so the cap was not checked: add-a-cluster advice below suggests no "
            "higher MAX_CLUSTER_COUNT for a multi-cluster warehouse.") in _captions(at)
    sized = _sized(at)
    assert "was not checked" in sized.loc["WH_SAT", "RATIONALE"]
    assert "raise MAX_CLUSTER_COUNT to 5" not in sized.loc["WH_SAT", "RATIONALE"]
    assert RECOMMEND_BELOW_CAP not in set(sized["RECOMMENDATION"])       # never judged on a failed read


def _pane(at) -> tuple[str, str]:
    """(every st.code block, markdown + captions) of the rendered page."""
    code = "\n".join(str(c.value) for c in at.code)
    text = " ".join(str(m.value) for m in at.markdown) + " " + _captions(at)
    return code, text


@_SKIP
@pytest.mark.parametrize(("check", "select", "raise_to_5"),
                         [(True, "WH_SAT", True), (True, "WH_LOW", False), (False, "WH_SAT", False)])
def test_the_operator_pane_prefills_only_a_reached_cap(monkeypatch, check, select, raise_to_5):
    """The review-only pane under the selected row: the MAX_CLUSTER_COUNT ALTER is shown only for a warehouse
    whose queries reached its cap (WH_SAT, checked); WH_LOW (checked, peak 3 of 4) gets the below-cap pane and
    no cluster statement; unchecked WH_SAT gets no statement and says why. The resize (size-up route) stays."""
    at, _seen = _page(monkeypatch, check=check, select=select)
    code, text = _pane(at)
    assert ("ALTER WAREHOUSE WH_SAT SET MIN_CLUSTER_COUNT = 1 MAX_CLUSTER_COUNT = 5;" in code) is raise_to_5
    assert f"ALTER WAREHOUSE {select} SET MIN_CLUSTER_COUNT" in code or not raise_to_5
    assert any(s.key == f"sizing_to_{select}" for s in at.selectbox)            # the resize is still offered
    if raise_to_5:
        assert "Scale-out fix (review-only)" in text
        assert "run it from Operations ▸ Emergency ▸ Cluster range (audited)." in text
        assert "queries reached cluster 4 of 4 in 57 hours of the last 35 days" in text
    elif select == "WH_LOW":
        assert "Cluster cap not reached (review-only)" in text and "Scale-out fix (review-only)" not in text
        assert "No MAX_CLUSTER_COUNT statement is generated: in the checked window no query reached" in text
        assert "MAX_CLUSTER_COUNT =" not in code
    else:
        assert "Scale-out fix (review-only)" in text and "MAX_CLUSTER_COUNT =" not in code
        assert ("Whether queries ever reach cluster 4 of 4 was not checked, so no MAX_CLUSTER_COUNT change is "
                "prefilled. No scale-out statement is generated here. The resize below is the size-up route.") in text


@_SKIP
def test_no_multi_cluster_warehouse_offers_no_check(monkeypatch):
    at, seen = _page(monkeypatch, check=True, max_clusters=1)
    assert ("No warehouse in this profile has MAX_CLUSTER_COUNT above 1 in SHOW WAREHOUSES, so there is no "
            "cluster cap to check.") in _captions(at)
    assert not any(t.key == "sizing_cluster_check" for t in at.toggle)
    assert not any("CLUSTER_NUMBER" in s for s in seen)
    assert "Single-cluster today (MAX_CLUSTER_COUNT = 1)" in _sized(at).loc["WH_SAT", "RATIONALE"]
