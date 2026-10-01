"""Next-Fifty #38 remainder on the RENDERED page (AppTest over the shaped harness, both CI legs):
Cost ▸ Optimization & Savings ▸ Idle & sizing with the right-sizing profile loaded and two queue-heavy
multi-cluster warehouses (max 4, STANDARD) -- WH_SAT, whose queries reached cluster 4 of 4, and WH_LOW,
whose peak was cluster 3.

  (a) the "Check cluster use" toggle OFF: no CLUSTER_NUMBER read is issued and WH_SAT's add-a-cluster
      rationale says its cluster cap was not checked;
  (b) ON: WH_SAT is told to raise MAX_CLUSTER_COUNT to 5; WH_LOW reads "Size up or split (cluster cap not
      reached)";
  (c) the read FAILS: a red "Query failed" line, the failed caption, and WH_SAT reads "was not checked";
  (d) no multi-cluster warehouse in SHOW: the no-cap caption and no toggle at all;
  (e) v4.604.0 review r1: an EMPTY SHOW says the cluster ranges are unknown (R1-10); a Small below-cap
      warehouse's resize picker opens on MEDIUM, a size-up (R1-4); the selected row's cluster columns name
      the check window (R1-9: the rendered headers and help, review r2 R2-8);
  (f) review r2 R2-2: a 2X-Large or 3X-Large below-cap warehouse (no size up in the picker) opens the picker on
      nothing, with no statement, saving or Execute until a size is picked;
  (g) review r3 R3-1: while the row stays selected, a size change (an Execute, or an outside resize) re-creates
      the picker on its new default, and a resize clears the typed confirm.

The shared shaped harness stubs SHOW WAREHOUSES as an empty frame, so only an injected frame reaches the gate
(tests/test_prc_c1_shaped.py renders that default). The fake-st and source twins add to these:
tests/test_cluster_cap_render.py calls the page's _cluster_cap_check with a
fake st for the gate's own paths -- (a), (c) the failed read, (d) and (e)'s empty SHOW -- and
tests/test_cluster_cap_gate.py locks the verdicts, the pure helpers behind (e)'s picker default and window
labels, and the page wiring by source."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
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

# the owner's W1c probe shapes: WH_TRXS_TRANSFORM's (57 hours at 4 of 4) and WH_ALFA_LOAD_PRD's (peak 3 of 4)
_HIST = {"WH_SAT": {1: 72, 2: 40, 3: 55, 4: 57}, "WH_LOW": {1: 300, 2: 20, 3: 3}}


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="t")


def _show(max_clusters: int, size: str = "X-Small") -> pd.DataFrame:
    return pd.DataFrame([{"name": n, "size": size, "min_cluster_count": 1, "max_cluster_count": max_clusters,
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
          select: str = "", size: str = "X-Small", show_empty: bool = False,
          show_size: dict | None = None) -> tuple[AppTest, list[str]]:
    """``show_size`` ({"size": ...}) is read by SHOW WAREHOUSES on every run, so a test can change the size between
    reruns while the row stays selected (review r3 R3-1); otherwise every run reads ``size``."""
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
            return _ok(pd.DataFrame() if show_empty else _show(max_clusters, show_size["size"] if show_size else size))
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


def _pick(at, warehouse: str = "WH_LOW"):
    """The Resize to picker. Its key is the warehouse, then the current size and the default index (review r3
    R3-1), so it is found by the warehouse prefix."""
    picks = [s for s in at.selectbox if str(s.key).startswith(f"sizing_to_{warehouse}_")]
    assert len(picks) == 1, [s.key for s in at.selectbox]
    return picks[0]


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


def test_toggle_on_gates_the_advice_on_hours_at_the_cap(monkeypatch):
    at, seen = _page(monkeypatch, check=True)
    reads = [s for s in seen if "CLUSTER_NUMBER" in s]
    assert reads and all("UPPER(q.WAREHOUSE_NAME) IN ('WH_LOW', 'WH_SAT')" in s for s in reads)
    # review r1 R1-7: from midnight (account time) 35 days back, never now minus 35 days
    assert all("DATEADD('day', -35, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)" in s
               for s in reads)
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
    assert _pick(at, select) is not None                                        # the resize is still offered
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


def test_no_multi_cluster_warehouse_offers_no_check(monkeypatch):
    at, seen = _page(monkeypatch, check=True, max_clusters=1)
    assert ("No warehouse in this profile has MAX_CLUSTER_COUNT above 1 in SHOW WAREHOUSES, so there is no "
            "cluster cap to check.") in _captions(at)
    assert not any(t.key == "sizing_cluster_check" for t in at.toggle)
    assert not any("CLUSTER_NUMBER" in s for s in seen)
    assert "Single-cluster today (MAX_CLUSTER_COUNT = 1)" in _sized(at).loc["WH_SAT", "RATIONALE"]


def test_an_empty_show_says_the_cluster_ranges_are_unknown(monkeypatch):
    """Review r1 R1-10: zero SHOW rows are an absent input — never "no warehouse has MAX_CLUSTER_COUNT above 1"."""
    at, seen = _page(monkeypatch, check=True, show_empty=True)
    info = " ".join(str(i.value) for i in at.info)
    assert "cluster ranges are unknown and the cluster-cap check cannot run" in info
    assert "cluster cap to check" not in _captions(at)
    assert not any(t.key == "sizing_cluster_check" for t in at.toggle)
    assert not any("CLUSTER_NUMBER" in s for s in seen)
    assert "the current setting is unknown" in _sized(at).loc["WH_SAT", "RATIONALE"]


def test_a_small_below_cap_warehouse_opens_the_resize_on_a_size_up(monkeypatch):
    """Review r1 R1-4: the below-cap pane calls the resize the size-up route, so the picker opens on MEDIUM for a
    Small warehouse (it opened on XSMALL: a downsize projecting a saving). Review r1 R1-9: the selected row's
    cluster columns name the check window, and the evidence frame (the CSV) carries it."""
    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", size="Small")
    pick = _pick(at)
    assert pick.value == "MEDIUM"
    assert list(pick.options) == ["XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE", "XXLARGE"]
    code, text = _pane(at)
    assert "ALTER WAREHOUSE WH_LOW SET WAREHOUSE_SIZE = 'MEDIUM';" in code
    assert "Resizing UP SMALL → MEDIUM raises cost — no saving booked." in text
    assert "Projected saving" not in text
    evidence = [df for df in at.dataframe
                if isinstance(df.value, pd.DataFrame) and "CLUSTER_CHECK_DAYS" in df.value.columns
                and "RATIONALE" in df.value.columns and len(df.value) == 1]
    assert evidence and evidence[0].value.iloc[0]["CLUSTER_CHECK_DAYS"] == 35
    assert evidence[0].value.iloc[0]["PEAK_CLUSTERS"] == 3
    # review r2 R2-8: the RENDERED headers name the check window, with the not-the-sizing-window help (a
    # `_cd = None` or a dropped help passed every test before)
    cfg = json.loads(evidence[0].proto.columns)
    assert cfg["PEAK_CLUSTERS"]["label"] == "Peak cluster (last 35 days)"
    assert cfg["AT_CAP_HOUR_COUNT"]["label"] == "Hours at cap (last 35 days)"
    for col in ("PEAK_CLUSTERS", "AT_CAP_HOUR_COUNT"):
        assert "not the sizing window" in cfg[col]["help"], col


@pytest.mark.parametrize(("size", "note"), [
    ("3X-Large", "This warehouse (3XLARGE) is larger than every size offered here, so every option is a downsize"),
    ("2X-Large", "The next size up from XXLARGE is not offered here, so every option is this size (no change)"),
])
def test_no_size_up_to_offer_opens_the_picker_on_nothing(monkeypatch, size, note):
    """Review r2 R2-2 / R2-7: a below-cap warehouse with no size up in the picker opened on XXLARGE — for a
    3X-Large warehouse a DOWNSIZE that showed and logged a projected saving under a 'size-up route' caption, for
    a 2X-Large one a no-op. It now opens with nothing picked: the note says why, and no statement, saving or
    Execute renders until the operator picks a size."""
    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", size=size)
    pick = _pick(at)
    assert pick.value is None
    assert list(pick.options) == ["XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE", "XXLARGE"]
    code, text = _pane(at)
    assert "Cluster cap not reached (review-only)" in text
    assert note in text and "The picker opens with no size picked: pick one to see the statement." in text
    assert "WAREHOUSE_SIZE" not in code
    assert "Projected saving" not in text and "Resizing UP" not in text and "2XLARGE" not in text
    assert not any(t.key == "sizing_confirm" for t in at.text_input)
    assert not any(b.key == "sizing_btn" for b in at.button)


def _recording_writes(monkeypatch) -> list[str]:
    """Every statement the Execute gate runs (the harness's own stub answers (True, 'stubbed') and keeps none)."""
    import app.ui.pages.cost_parts.optimize as opt

    writes: list[str] = []

    def _execute(sql, **_kwargs):
        writes.append(str(sql))
        return True, "Statement executed."

    monkeypatch.setattr(opt, "execute_statement", _execute)
    return writes


def _resize(at, writes: list[str], to: str) -> None:
    """Type the warehouse name and click Execute: the ALTER to ``to`` runs and is logged."""
    at.text_input(key="sizing_confirm").input("WH_LOW").run()
    at.button(key="sizing_btn").click().run()
    assert not at.exception
    assert f"ALTER WAREHOUSE WH_LOW SET WAREHOUSE_SIZE = '{to}';" in writes
    assert any("REMEDIATION_LOG" in w for w in writes)


def test_the_picker_follows_its_default_when_the_size_changes_under_a_selected_row(monkeypatch):
    """Review r3 R3-1: the picker was keyed on the warehouse alone and Streamlit leaves `index` out of a keyed
    selectbox's identity, so while WH_LOW stayed selected an earlier value outlived a new default. After an
    Execute (X-Large -> 2X-Large) the pane said no size was picked beside an ALTER to XXLARGE and an enabled
    Execute; after an outside resize (Large -> 3X-Large) it projected a saving for a downsize nobody picked. Each
    size change now opens the picker on its own default: nothing, or the new size up."""
    show = {"size": "X-Large"}
    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", show_size=show)
    writes = _recording_writes(monkeypatch)
    assert _pick(at).value == "XXLARGE"
    _resize(at, writes, "XXLARGE")
    for size, up in (("2X-Large", None), ("Large", "XLARGE"), ("3X-Large", None)):
        show["size"] = size
        at.run()
        assert not at.exception, size
        assert _pick(at).value == up, size
        code, text = _pane(at)
        assert "Projected saving" not in text, size
        if up is None:
            assert "WAREHOUSE_SIZE" not in code, size
            assert "The picker opens with no size picked: pick one to see the statement." in text, size
            assert not any(b.key == "sizing_btn" for b in at.button), size
        else:
            assert [ln for ln in code.splitlines() if "WAREHOUSE_SIZE" in ln] == [
                f"ALTER WAREHOUSE WH_LOW SET WAREHOUSE_SIZE = '{up}';"]
            assert f"Resizing UP LARGE → {up} raises cost — no saving booked." in text
            assert "The picker opens with no size picked" not in text


def test_a_resize_clears_the_typed_confirm(monkeypatch):
    """Review r3 R3-1: after a resize (Large -> XLARGE) the picker opens on the NEW size up, XXLARGE, so the name
    typed for the first resize is cleared: a second Execute needs a new confirm."""
    show = {"size": "Large"}
    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", show_size=show)
    writes = _recording_writes(monkeypatch)
    assert _pick(at).value == "XLARGE"
    _resize(at, writes, "XLARGE")
    show["size"] = "X-Large"
    at.run()
    assert not at.exception
    assert _pick(at).value == "XXLARGE"
    assert at.text_input(key="sizing_confirm").value == ""
    assert at.button(key="sizing_btn").disabled
