"""Next-Fifty #27 — the below-warehouse drill's wiring on Cost ▸ Spend & Attribution.

Source locks: the helper is called inside the root-cause waterfall expander; nothing reads until its
own toggle is on (an expander body runs every rerun, even collapsed); the Operations jump is gated on
can_open; the helper adds no ACCOUNT_USAGE literal, no raw st.info/st.success and no second
reconciliation_footer; a capped frame is never explained. Plus an AppTest render: toggle off -> no
batch read at all; toggle on -> the narrative, the tables, the change bullet and the jump render.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd
import pytest

from tests._source import read

_SPEND = "app/ui/pages/cost_parts/spend.py"


def _helper() -> str:
    return read(_SPEND).split("def _below_warehouse_drill(", 1)[1].split("\ndef ", 1)[0]


# ---------------------------------------------------------------------------
# Source locks
# ---------------------------------------------------------------------------

def test_called_inside_the_root_cause_waterfall_expander():
    src = read(_SPEND)
    assert "explain_by_warehouse(" in src and "root-cause waterfall" in src      # rec5 lock kept
    block = src.split('root-cause waterfall",', 1)[1].split("How to investigate a flag", 1)[0]
    assert '_below_warehouse_drill(company, exp, str(_top["label"]), rate)' in block
    # the helper sits AFTER _attribution_tab (test_bughunt_round13 slices _spend_tab.._attribution_tab)
    assert (src.index("def _attribution_tab(") < src.index("def _below_warehouse_drill(")
            < src.index("def _account_storage_tiers("))


def test_nothing_reads_until_the_toggle_is_on():
    body = _helper()
    gate = body.index('st.toggle("Break down by user, database and setting changes", '
                      'key="spend_anom_below_wh_load"')
    for read_call in ("run_batch(", "mart27_sql.alloc_xdim_day_drivers(",
                      "change_impact_sql.warehouse_change_registry(", "user_display_map("):
        assert gate < body.index(read_call), read_call
    assert "run(" not in body.replace("run_batch(", "")                  # one batch, no serial reads
    assert 'tier="hourly"' in body
    assert "_ANOM_CHANGE_LOOKBACK_DAYS, company, wh)" in body
    # the pre-toggle code (warehouse pick + jump, after the docstring) is widget-only
    pre = body[:gate].split('"""', 2)[2]
    for tok in ("run(", "run_batch(", "_sql.", "user_display_map(", "load_settings("):
        assert tok not in pre, tok


def test_jump_is_gated_on_can_open_and_carries_the_warehouse():
    body = _helper()
    btn = body.index('can_open("Operations") and st.button(')
    nav = body.index('request_navigation("Operations", "Queries", {"warehouse_contains": wh})')
    assert btn < nav
    assert 'key="spend_anom_open_queries"' in body
    assert 'st.selectbox("Warehouse to break down", opts, key=f"spend_anom_drill_wh_{fday}")' in body


def test_helper_budgets_and_honesty():
    body = _helper()
    assert "ACCOUNT_USAGE" not in body                                   # spend.py stays at its budget
    assert "st.info(" not in body and "st.success(" not in body          # spend.py raw ceiling
    assert "reconciliation_footer(" not in body                          # spend.py keeps exactly one
    assert "st.dataframe(" not in body
    assert "if xd.truncated:" in body                                    # a capped frame is never explained
    assert "credits_to_usd(safe_float(c), rate, round_cents=False)" in body
    assert 'status_chips([("Allocated estimate", ""), ("Usage basis — not billed", "")])' in body
    assert "st.markdown(md_dollars(below.narrative))" in body
    assert "median" in body and "zero" in body                           # the method caption says why it differs
    assert read(_SPEND).count("reconciliation_footer(") == 1


# ---------------------------------------------------------------------------
# AppTest render
# ---------------------------------------------------------------------------

def _xdim_frame(fday) -> pd.DataFrame:
    rows = []
    for o in range(14, -1, -1):
        d = fday - timedelta(days=o)
        rows.append({"DAY": d, "DIMENSION": "SPINE", "KEY_NAME": "", "CREDITS": 0.0})
        alloc = 10.0
        rows += [{"DAY": d, "DIMENSION": "USER", "KEY_NAME": "BOB_B", "CREDITS": 10.0},
                 {"DAY": d, "DIMENSION": "DATABASE", "KEY_NAME": "DB_SALES", "CREDITS": 10.0}]
        if o in (0, 4, 9):
            rows += [{"DAY": d, "DIMENSION": "USER", "KEY_NAME": "CAROL_C", "CREDITS": 80.0},
                     {"DAY": d, "DIMENSION": "DATABASE", "KEY_NAME": "DB_ADHOC", "CREDITS": 80.0}]
            alloc += 80.0
        rows.append({"DAY": d, "DIMENSION": "METERED", "KEY_NAME": "WH_A", "CREDITS": alloc + 2.0})
    return pd.DataFrame(rows)


def _render(monkeypatch, *, toggle_on: bool):
    # No ButtonGroup widget on this path, so it also renders on the streamlit 1.52 floor leg.
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.core.result import QueryResult
    from app.logic.formulas import account_today
    from app.ui.pages.cost_parts import spend

    fday = account_today() - timedelta(days=1)
    empty = QueryResult(df=pd.DataFrame(), ok=True, source="stub")
    xdim = QueryResult(df=_xdim_frame(fday), ok=True, source="xdim stub")
    chg = QueryResult(df=pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_A", "COMPANY": "ALFA", "SETTING": "SIZE", "OLD_VALUE": "MEDIUM",
        "NEW_VALUE": "LARGE", "CHANGE_SEEN_AT": datetime.combine(fday, time(6, 40)),
        "CHANGED_BY": "DBA_JANE", "CHANGE_SOURCE": "MANUAL", "VERDICT": "", "VERDICT_DETAIL": ""}]),
        ok=True, source="registry stub")
    batches: list[dict] = []
    nav: list[tuple] = []

    def _batch(specs, **kwargs):
        batches.append({"specs": list(specs), **kwargs})
        return {s["key"]: {"xdim": xdim, "whchg": chg}.get(s["key"], empty) for s in specs}

    monkeypatch.setattr(spend, "run", lambda *a, **k: empty)
    monkeypatch.setattr(spend, "run_mart_first", lambda *a, **k: empty)
    monkeypatch.setattr(spend, "run_batch", _batch)
    monkeypatch.setattr(spend, "load_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(spend, "user_display_map", lambda *_a, **_k: {"CAROL_C": "Carol Carter"})
    monkeypatch.setattr(spend, "can_open", lambda _page: True)
    monkeypatch.setattr(spend, "request_navigation", lambda *a, **k: nav.append((a, k)))
    days = [fday - timedelta(days=o) for o in range(21, 0, -1)] + [fday]
    daily = QueryResult(df=pd.DataFrame({
        "DAY": days, "WAREHOUSE_NAME": "WH_A", "COMPANY": "ALFA",
        "CREDITS_TOTAL": [33.4] * 21 + [300.0], "CREDITS_COMPUTE": [33.0] * 21 + [296.0]}),
        ok=True, source="daily stub")
    wh = QueryResult(df=pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "COMPANY": ["ALFA"],
                                      "CREDITS_CURRENT": [10.0], "CREDITS_PRIOR": [8.0]}),
                     ok=True, source="wh stub")
    # AppTest runs the function's SOURCE as a script (no closures): hand the frames over through a
    # module attribute the script imports (the tests/test_spend_grain_coverage.py pattern).
    monkeypatch.setattr(spend, "_BELOW_WH_TEST_ARGS",
                        {"wh_res": wh, "daily_res": daily, "grain_res": empty}, raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _spend._attribution_tab("ALL", 30, 3.0, **_spend._BELOW_WH_TEST_ARGS)

    at = AppTest.from_function(_app, default_timeout=60)
    at.session_state["spend_anom_below_wh_load"] = toggle_on
    at.run()
    assert not at.exception, at.exception
    return at, batches, nav, fday


def _markdown(at) -> str:
    return " ".join(str(m.value) for m in at.markdown)


def test_toggle_off_reads_nothing_but_offers_the_jump(monkeypatch):
    at, batches, nav, _ = _render(monkeypatch, toggle_on=False)
    assert batches == []                                                 # the expander body ran; no read
    assert "Why did" in " ".join(str(e.label) for e in at.expander)
    labels = [b.label for b in at.button]
    assert "Queries on WH_A → Operations ▸ Queries" in labels
    at.button(key="spend_anom_open_queries").click().run()
    assert nav == [(("Operations", "Queries", {"warehouse_contains": "WH_A"}), {})]


def test_toggle_on_explains_the_day_in_one_hourly_batch(monkeypatch):
    at, batches, _, fday = _render(monkeypatch, toggle_on=True)
    assert len(batches) == 1 and batches[0]["tier"] == "hourly"
    sqls = {s["key"]: s["sql"] for s in batches[0]["specs"]}
    assert set(sqls) == {"xdim", "whchg"}
    assert f"'{fday.isoformat()}'::DATE" in sqls["xdim"] and "x.WAREHOUSE_NAME = 'WH_A'" in sqls["xdim"]
    assert "WAREHOUSE_CHANGE_REGISTRY" in sqls["whchg"]
    md = _markdown(at)
    assert f"WH_A on {fday}:" in md and "By user: CAROL_C" in md and "By database: DB_ADHOC" in md
    assert "SIZE: MEDIUM → LARGE" in md and ": MANUAL" not in md
    users = [df.value for df in at.dataframe if "User" in getattr(df.value, "columns", [])]
    assert users and "Carol Carter" in users[0]["User"].tolist()        # directory name, not the login
    assert users[0]["User"].tolist()[-1].startswith("Not allocated to a query")
