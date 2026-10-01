"""Cost Intelligence (Spend & Attribution, Compare, Unmapped entities): honesty fixes.

Each test names the review finding it locks and fails on the code it replaced.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.data import mart_sql
from tests._source import read

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


# ------------------------------------------------------------ R1-145: unmapped worklist window ----
@pytest.mark.parametrize("days", [60, 90, 180, 365])
def test_unmapped_worklist_serves_the_page_window_not_30d(days):
    """The builder clamped to 30 days while the panel's chip, 'in this window' KPI help, 'Est. $ (window)'
    column and green clean state claimed the full window. All three sources are FACT_* tables, so the mart
    window cap applies."""
    sql = mart_sql.unmapped_entities(days)
    assert sql.count(f"DATEADD('day', -{days}, CURRENT_DATE())") == 3
    assert "DATEADD('day', -30," not in sql
    assert "ACCOUNT_USAGE" not in sql                                   # still mart-only


def test_unmapped_worklist_honors_calendar_bounds():
    """Last month read a trailing 30 days (Aug 31 - Sep 30, one day of the labelled month)."""
    sql = mart_sql.unmapped_entities(31, bounds=(date(2026, 8, 1), date(2026, 9, 1)))
    assert "DAY >= '2026-08-01' AND DAY < '2026-09-01'" in sql
    assert "HOUR_TS >= '2026-08-01' AND HOUR_TS < '2026-09-01'" in sql
    assert "DATEADD(" not in sql
    # the default (canary) shape is unchanged
    assert mart_sql.unmapped_entities(7).count("DATEADD('day', -7, CURRENT_DATE())") == 3


def test_cost_page_passes_the_window_bounds_to_the_worklist():
    cost = read("app/ui/pages/cost.py")
    assert 'mart_sql.unmapped_entities(f["days"], bounds=f["bounds"])' in cost
    assert "key=f\"unmapped_{f['days']}{_unm_b}\"" in cost


# ------------------------------------------------------------ R1-146: mapper picks a row + grain ----
_STATEMENTS: list[str] = []


def _mapper_app():
    import pandas as _pd

    from app.ui.pages import cost as _cost

    _cost._unmapped_mapper(_pd.DataFrame({
        "GRAIN": ["DATABASE", "USER", "WAREHOUSE"],
        "ENTITY": ["ANALYTICS", "SVC_ETL", "ANALYTICS"],
        "MEASURE": ["queries", "logins", "credits"],
        "VALUE": [120.0, 4.0, 85.5],
    }), True)


def _drive_mapper(monkeypatch, option_index: int):
    from app.ui.pages import cost

    _STATEMENTS.clear()

    def _record_statement(sql, *a, **k):
        _STATEMENTS.append(str(sql))
        return True, "ok"

    monkeypatch.setattr(cost, "execute_statement", _record_statement)
    at = AppTest.from_function(_mapper_app, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    entity = next(s for s in at.selectbox if s.label == "Entity")
    entity.set_value(option_index)   # the raw row index (AppTest re-formats it via format_func)
    at.run()
    at.button(key="unmap_apply").click()
    at.run()
    assert not at.exception, at.exception
    return at


def test_mapper_offers_each_row_with_its_grain(monkeypatch):
    at = _drive_mapper(monkeypatch, 0)
    entity = next(s for s in at.selectbox if s.label == "Entity")
    assert list(entity.options) == ["ANALYTICS · Database", "SVC_ETL · User", "ANALYTICS · Warehouse"]


def test_mapper_writes_the_warehouse_grain_when_the_warehouse_row_is_picked(monkeypatch):
    """Same-name DATABASE + WAREHOUSE rows: the old name picker could not select the second entry and
    the name lookup took the DATABASE row, so Apply MERGEd a DATABASE scope and the warehouse stayed
    UNKNOWN (billed blind)."""
    at = _drive_mapper(monkeypatch, 2)
    assert len(_STATEMENTS) == 1, _STATEMENTS
    assert "'WAREHOUSE' AS SCOPE_TYPE, 'ANALYTICS' AS PATTERN" in _STATEMENTS[0]
    assert "Classified via OVERWATCH (WAREHOUSE)" in _STATEMENTS[0]
    caps = " ".join(str(c.value) for c in at.caption)
    assert "Maps **ANALYTICS** (WAREHOUSE)" in caps


def test_mapper_user_row_maps_through_user_override(monkeypatch):
    _drive_mapper(monkeypatch, 1)
    assert "'USER_OVERRIDE' AS SCOPE_TYPE, 'SVC_ETL' AS PATTERN" in _STATEMENTS[0]


def test_mapper_latch_key_and_receipt_carry_the_grain():
    cost = read("app/ui/pages/cost.py")
    assert cost.count('f"unmap_apply:{scope_type}:{pick}:{company_choice}"') == 2
    assert 'f"Mapped {pick} ({scope_type}) → {company_choice}."' in cost


# --------------------------------------------- R1-150 / R1-151 / R1-152: Compare honesty ----
_KPIS: list[list[dict]] = []
_TABLES: list[pd.DataFrame] = []


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="stub")


def _fail(kind: str) -> QueryResult:
    return QueryResult(ok=False, error=f"boom ({kind})", error_kind=kind, source="stub")


_EMPTY = _ok(pd.DataFrame())


def _wh(b_days: float = 30.0, b_credits: float = 80.0, a_days: float = 30.0,
        a_credits: float = 100.0) -> QueryResult:
    return _ok(pd.DataFrame({
        "WAREHOUSE_NAME": ["WH_A"], "A_CREDITS": [a_credits], "B_CREDITS": [b_credits],
        "A_DAYS": [a_days], "B_DAYS": [b_days], "TOTAL_A_CREDITS": [a_credits], "TOTAL_B_CREDITS": [b_credits],
        "LOADED_THROUGH": ["2099-01-01"]}))


def _act(*sides: tuple) -> QueryResult:
    return _ok(pd.DataFrame([{"SIDE": sd, "QUERIES": q, "FAILS": f, "QUEUED_SEC": qu, "SPILL_REMOTE_GB": 0.0}
                             for sd, q, f, qu in sides]))


def _render_compare(monkeypatch, *, wh=None, act=None, bill=None, pat=None):
    from app.ui.pages.cost_parts import compare

    batch = {"wh": wh or _wh(),
             "act": act if act is not None else _act(("A", 1000.0, 25.0, 300.0), ("B", 900.0, 10.0, 200.0)),
             "bill": bill if bill is not None else _EMPTY, "pat": pat if pat is not None else _EMPTY}
    _KPIS.clear()
    _TABLES.clear()

    def _no_serial_read(*_a, **_k):
        raise AssertionError("no serial read expected: run_batch returns every key")

    monkeypatch.setattr(compare, "run_batch", lambda *a, **k: dict(batch))
    monkeypatch.setattr(compare, "run", _no_serial_read)
    monkeypatch.setattr(compare, "kpi_row", lambda items, *a, **k: _KPIS.append(list(items)))
    monkeypatch.setattr(compare, "styled_table", lambda df, *a, **k: _TABLES.append(df.copy()))

    def _app():
        from app.ui.pages.cost_parts import compare as _compare
        _compare._compare_tab("ALL", 3.0, 3.0)

    at = AppTest.from_function(_app, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def _kpi(label_start: str) -> dict:
    return next(k for k in (_KPIS[-1] if _KPIS else []) if k["label"].startswith(label_start))


def test_compare_fail_rate_never_fabricates_a_zero_b_side(monkeypatch):
    """R1-150: an A-only activity frame drew '+2.50 pts vs B' (red) against a B with no queries, beside a
    Queries card that said 'no B-side data'."""
    _render_compare(monkeypatch, act=_act(("A", 1000.0, 25.0, 300.0)))
    fr = _kpi("Fail rate")
    assert fr["value"] == "2.50%"
    assert fr["delta"] == "no B-side data" and fr["delta_color"] == "off"
    # the help says what the delta says: B has no rows at all (not 'no queries in the B window')
    assert "0.00%" not in fr["help"] and "no B-side data" in fr["help"] and "no queries" not in fr["help"]
    assert _kpi("Queries")["delta"] == "no B-side data"
    assert _kpi("Queries")["help"].startswith("B = —.")                     # not a fabricated 'B = 0'
    assert _kpi("Queued")["delta"] == "no B-side data" and _kpi("Queued")["delta_color"] == "off"
    assert _kpi("Queued")["help"] == "B = —."
    # the Volume shape table shows the absent side as a dash, never a fabricated 0
    vol = _TABLES[-1]
    assert list(vol["B"]) == ["—"] * 4 and vol["DELTA_PCT"].isna().all()


def test_compare_fail_rate_with_no_a_side_is_a_dash(monkeypatch):
    """R1-150: with no A row, Queries and Queued showed a made-up 0 and '-100.0% vs B' -- GREEN on the
    inverse Queued chip, i.e. 'A better than B' claimed on missing data."""
    _render_compare(monkeypatch, act=_act(("B", 900.0, 10.0, 200.0)))
    fr = _kpi("Fail rate")
    assert fr["value"] == "—" and fr["delta"] == "no A-side data" and fr["delta_color"] == "off"
    for label in ("Queries", "Queued"):
        k = _kpi(label)
        assert k["value"] == "—", (label, k)
        assert k["delta"] == "no A-side data" and k["delta"] != "-100.0% vs B", (label, k)
        assert k["delta_color"] == "off", (label, k)
        assert "—" not in k["help"], (label, k)                                # B is real: show it
    # the Volume shape table agrees: the absent A side is a dash, never a fabricated 0
    vol = _TABLES[-1]
    assert list(vol["A"]) == ["—"] * 4 and vol["DELTA_PCT"].isna().all()


def test_compare_a_loaded_with_zero_queries_says_no_a_side_queries(monkeypatch):
    """A LOADED A side with zero queries is a real 0 (a % change against B is honest); only its fail rate
    has no denominator."""
    _render_compare(monkeypatch, act=_act(("A", 0.0, 0.0, 0.0), ("B", 900.0, 10.0, 200.0)))
    fr = _kpi("Fail rate")
    assert fr["value"] == "—" and fr["delta"] == "no A-side queries" and fr["delta_color"] == "off"
    assert _kpi("Queries")["value"] == "0" and _kpi("Queries")["delta"] == "-100.0% vs B"


def test_compare_b_loaded_with_zero_queries_help_says_no_queries(monkeypatch):
    _render_compare(monkeypatch, act=_act(("A", 1000.0, 25.0, 300.0), ("B", 0.0, 0.0, 0.0)))
    fr = _kpi("Fail rate")
    assert fr["delta"] == "no B-side queries" and fr["delta_color"] == "off"
    assert fr["help"] == "B = — (no queries in the B window)."


def test_compare_billed_and_warehouse_with_no_a_side_are_dashes(monkeypatch):
    """R1-150, same class on the money cards: a missing A side is never '$0.00, -100.0% vs B' (green)."""
    bill = _ok(pd.DataFrame([{"SIDE": "B", "CREDITS_BILLED": 100.0, "CREDITS_BILLED_AI": 0.0,
                              "CREDITS_BILLED_OTHER": 100.0}]))
    _render_compare(monkeypatch, wh=_wh(a_days=0.0, a_credits=0.0), bill=bill)
    for label in ("Warehouse spend", "Account billed"):
        k = _kpi(label)
        assert k["value"] == "—", (label, k)
        assert k["delta"] == "no A-side data" and k["delta_color"] == "off", (label, k)
        assert "B = $" in k["help"], (label, k)


def test_compare_billed_with_no_b_side_shows_a_dash_for_b(monkeypatch):
    bill = _ok(pd.DataFrame([{"SIDE": "A", "CREDITS_BILLED": 100.0, "CREDITS_BILLED_AI": 0.0,
                              "CREDITS_BILLED_OTHER": 100.0}]))
    _render_compare(monkeypatch, bill=bill)
    k = _kpi("Account billed")
    assert k["delta"] == "no B-side data" and k["delta_color"] == "off"
    assert k["help"].endswith("B = —.") and "B = $0.00" not in k["help"]


def test_delta_chip_a_absence_wins_over_the_fabricated_minus_100():
    from app.ui.pages.cost_parts.compare import _chip_color, _delta_chip

    assert _delta_chip(0.0, 50.0, a_present=False) == "no A-side data"
    assert _chip_color(_delta_chip(0.0, 50.0, a_present=False), "inverse") == "off"
    assert _delta_chip(0.0, 50.0) == "-100.0% vs B"                       # a LOADED A of 0 is a real move


def test_compare_a_loaded_zero_b_is_not_missing_data(monkeypatch):
    """R1-150: B loaded with 0s queued read 'no B-side data'."""
    _render_compare(monkeypatch, act=_act(("A", 1000.0, 25.0, 300.0), ("B", 900.0, 10.0, 0.0)))
    q = _kpi("Queued")
    assert q["delta"] == "up from 0 vs B" and q["delta_color"] == "inverse"
    fr = _kpi("Fail rate")
    assert fr["delta"] == f"{2.5 - 10 / 9:+.2f} pts vs B" and fr["delta_color"] == "inverse"


def test_compare_warehouse_spend_b_presence_comes_from_coverage(monkeypatch):
    _render_compare(monkeypatch, wh=_wh(b_days=0.0, b_credits=0.0))
    w = _kpi("Warehouse spend")
    assert w["delta"] == "no B-side data" and w["delta_color"] == "off"


def _errors(at) -> str:
    return " | ".join(str(e.value) for e in at.error)


def _infos(at) -> str:
    return " | ".join(str(e.value) for e in at.info)


@pytest.mark.parametrize("kind", ["timeout", "missing_column", "other"])
def test_compare_failed_pattern_read_is_unavailable_not_v037(monkeypatch, kind):
    """R1-151: any pattern-movers failure said 'need migration V037' (installed since v4.37) and hid the error."""
    at = _render_compare(monkeypatch, pat=_fail(kind))
    assert "V037" not in _infos(at) and "MART_PATTERN_COST_DAILY" not in _infos(at)
    assert f"boom ({kind})" in _errors(at)


def test_compare_absent_pattern_mart_is_needs_setup(monkeypatch):
    at = _render_compare(monkeypatch, pat=_fail("absent"))
    assert "MART_PATTERN_COST_DAILY" in _infos(at) and "boom" not in _errors(at)


def test_compare_failed_activity_and_billed_reads_are_named(monkeypatch):
    """R1-152: a failed act / bill read silently dropped its KPIs (and the Volume shape) with no message."""
    at = _render_compare(monkeypatch, act=_fail("timeout"), bill=_fail("timeout"))
    errs = _errors(at)
    assert "Query activity (FACT_QUERY_HOURLY) could not be read" in errs
    assert "Account billed credits (FACT_METERING_DAILY) could not be read" in errs
    assert [k["label"].split(" — ", 1)[0] for k in _KPIS[-1]] == ["Warehouse spend"]


def test_compare_empty_activity_says_so_under_volume_shape(monkeypatch):
    at = _render_compare(monkeypatch, act=_EMPTY)
    assert any("Volume shape" in str(m.value) for m in at.markdown)
    assert any("No query activity in either window yet" in str(c.value) for c in at.caption)


# ------------------------------------------------- R1-151 (optimize half): savings ledger ----
@pytest.mark.parametrize("kind", ["timeout", "missing_column", "other"])
def test_savings_ledger_failure_is_unavailable(monkeypatch, kind):
    from app.ui.pages.cost_parts import optimize
    from tests.test_probe_absence_split import _failed, _patch

    _fake, seen = _patch(monkeypatch, optimize, {"savings_ledger": _failed(kind)})
    optimize._savings_tab(3.0, {})
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "not installed" not in msg
    assert seen["detail"] == [f"boom ({kind})"]


@pytest.mark.parametrize("kind", ["absent", "privilege", "unknown_function"])
def test_savings_ledger_absence_is_needs_setup(monkeypatch, kind):
    from app.ui.pages.cost_parts import optimize
    from tests.test_probe_absence_split import _failed, _patch

    _fake, seen = _patch(monkeypatch, optimize, {"savings_ledger": _failed(kind)})
    optimize._savings_tab(3.0, {})
    ((state, msg),) = seen["empty"]
    assert state == "needs_setup"
    # R1-151: SETUP_ABSENCE_KINDS includes 'privilege' (the table exists; SAVINGS_LEDGER since V005), so the
    # wording is the neutral compare.py one -- never 'not installed' / 'apply the pending schema update'.
    assert "not installed" not in msg and "apply the pending schema update" not in msg
    assert "SAVINGS_LEDGER" in msg and "isn't readable by this app" in msg


# ------------------------------------------------------- Spend & Attribution (spend.py) ----
def _attribution(monkeypatch, *, daily, chg=None, toggles=(), app=None):
    """Drive the real spend._attribution_tab under AppTest (the tests/test_spend_grain_coverage.py and
    tests/test_spend_below_warehouse_wiring.py pattern): every other read is ok-empty."""
    from app.ui.pages.cost_parts import spend
    from tests.test_spend_below_warehouse_wiring import _xdim_frame

    empty = _ok(pd.DataFrame())
    charts_seen: list[pd.DataFrame] = []

    def _batch(specs, **_k):
        out = {}
        for s in specs:
            if s["key"] == "xdim":
                out["xdim"] = _ok(_xdim_frame(_flag_day()))
            elif s["key"] == "whchg":
                out["whchg"] = chg if chg is not None else empty
            else:
                out[s["key"]] = empty
        return out

    def _mart_first(*_a, key: str = "", **_k):
        return app if (app is not None and key.startswith("app_cost_")) else empty

    monkeypatch.setattr(spend, "run", lambda *a, **k: empty)
    monkeypatch.setattr(spend, "run_mart_first", _mart_first)
    monkeypatch.setattr(spend, "run_batch", _batch)
    monkeypatch.setattr(spend, "load_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(spend, "user_display_map", lambda *_a, **_k: {})
    monkeypatch.setattr(spend.charts, "bar_usd", lambda df, *a, **k: charts_seen.append(df.copy()))
    wh = _ok(pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "COMPANY": ["ALFA"],
                           "CREDITS_CURRENT": [10.0], "CREDITS_PRIOR": [8.0]}))
    monkeypatch.setattr(spend, "_C07_TEST_ARGS", {"wh_res": wh, "daily_res": daily, "grain_res": empty},
                        raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _spend._attribution_tab("ALL", 30, 3.0, **_spend._C07_TEST_ARGS)

    at = AppTest.from_function(_app, default_timeout=60)
    for key in toggles:
        at.session_state[key] = True
    at.run()
    assert not at.exception, at.exception
    return at, charts_seen


def _flag_day():
    from datetime import timedelta

    from app.logic.formulas import account_today
    return account_today() - timedelta(days=1)


def _spiking_daily() -> QueryResult:
    from datetime import timedelta
    fday = _flag_day()
    days = [fday - timedelta(days=o) for o in range(21, 0, -1)] + [fday]
    return _ok(pd.DataFrame({"DAY": days, "WAREHOUSE_NAME": "WH_A", "COMPANY": "ALFA",
                             "CREDITS_TOTAL": [33.4] * 21 + [300.0], "CREDITS_COMPUTE": [33.0] * 21 + [296.0]}))


def _captions(at) -> str:
    return " | ".join(str(c.value) for c in at.caption)


_LOADING = "Anomaly flags appear once 30 days of per-warehouse daily facts have loaded."


@pytest.mark.parametrize("kind", ["timeout", "missing_column", "other"])
def test_failed_daily_anomaly_read_is_unavailable_not_loading(monkeypatch, kind):
    """R1-077 / R1-160: a failed FACT_WAREHOUSE_DAILY read rendered the 'appear once ... loaded' caption, so
    the spike/collapse check silently switched off and read as 'no data yet'."""
    at, _ = _attribution(monkeypatch, daily=_fail(kind))
    assert _LOADING not in _captions(at)
    assert "The daily anomaly check could not read FACT_WAREHOUSE_DAILY" in _errors(at)
    assert any(f"boom ({kind})" in str(c.value) for c in at.code)          # the detail expander


def test_absent_daily_fact_is_needs_setup_and_empty_is_loading(monkeypatch):
    at, _ = _attribution(monkeypatch, daily=_fail("absent"))
    assert "FACT_WAREHOUSE_DAILY, which isn't readable" in _infos(at) and _LOADING not in _captions(at)
    at, _ = _attribution(monkeypatch, daily=_ok(pd.DataFrame()))
    assert _LOADING in _captions(at) and not _errors(at)


def test_failed_change_registry_read_is_unavailable_with_detail(monkeypatch):
    """R1-160: the below-warehouse drill showed a bare caption for a failed WAREHOUSE_CHANGE_REGISTRY read."""
    at, _ = _attribution(monkeypatch, daily=_spiking_daily(), chg=_fail("timeout"),
                         toggles=("spend_anom_below_wh_load",))
    assert "Warehouse setting changes could not be read for this day." in _errors(at)
    assert "isn't readable here right now" not in _captions(at)
    assert any("boom (timeout)" in str(c.value) for c in at.code)


def _app_frame(n_users: int = 300) -> pd.DataFrame:
    rows = [{"APPLICATION": "Snowsight", "USER_NAME": f"U{i}", "COMPANY": "ALFA", "QUERIES": 10.0,
             "CREDITS": 2.0} for i in range(n_users)]
    rows += [{"APPLICATION": "JDBC", "USER_NAME": f"J{i}", "COMPANY": "ALFA", "QUERIES": 5.0,
              "CREDITS": 1.8} for i in range(n_users)]
    rows += [{"APPLICATION": "ODBC", "USER_NAME": f"O{i}", "COMPANY": "ALFA", "QUERIES": 5.0,
              "CREDITS": 1.7} for i in range(1000 - 2 * n_users)]
    return pd.DataFrame(rows)        # exactly the 1,000-row LIMIT; Tableau's 400 x 1.6 rows fell past it


def test_app_cost_totals_from_a_capped_frame_are_disclosed(monkeypatch):
    """R1-157: per-application totals summed a LIMIT-1000 application x user x company frame and the caption
    still said the ranking was reliable."""
    at, charts_seen = _attribution(monkeypatch, daily=_ok(pd.DataFrame()), toggles=("spend_app_cost_load",),
                                   app=_ok(_app_frame()))
    caps = _captions(at)
    assert "LOWER BOUND" in caps and "1,000-row cap" in caps
    assert "Ranking by program is reliable" not in caps
    assert charts_seen and set(charts_seen[-1]["APPLICATION"]) == {"Snowsight", "JDBC", "ODBC"}


def test_app_cost_totals_use_the_pre_limit_application_totals(monkeypatch):
    """With the builder's pre-LIMIT APP_CREDITS / APP_QUERIES (review R1-031) the chart is exact: Tableau,
    whose every user row fell past the cap, is charted at its true total and ranks first."""
    df = _app_frame()
    totals = {"Snowsight": (600.0, 3000.0), "JDBC": (540.0, 1500.0), "ODBC": (680.0, 2000.0),
              "Tableau": (640.0, 400.0)}
    df["APP_CREDITS"] = df["APPLICATION"].map(lambda a: totals[a][0])
    df["APP_QUERIES"] = df["APPLICATION"].map(lambda a: totals[a][1])
    tab = pd.DataFrame([{"APPLICATION": "Tableau", "USER_NAME": "T0", "COMPANY": "ALFA", "QUERIES": 1.0,
                         "CREDITS": 1.6, "APP_CREDITS": 640.0 * 3, "APP_QUERIES": 400.0}])
    df = pd.concat([df, tab], ignore_index=True)
    at, charts_seen = _attribution(monkeypatch, daily=_ok(pd.DataFrame()), toggles=("spend_app_cost_load",),
                                   app=_ok(df))
    chart = charts_seen[-1].set_index("APPLICATION")
    assert chart.loc["Tableau", "USD"] == pytest.approx(640.0 * 3 * 3.0)
    assert chart.loc["Snowsight", "USD"] == pytest.approx(600.0 * 3.0)
    assert next(iter(chart.index)) == "Tableau"
    assert "LOWER BOUND" not in _captions(at)


# ---------------------------------------------------- storage (R1-155 / R1-156) ----
_TIB = float(1024 ** 4)


def _storage(monkeypatch, *, bounds, fact_latest="2026-08-20", fact_tib=20 / 31):
    from app.logic import date_windows
    from app.ui.pages.cost_parts import spend

    today = date(2026, 9, 30)
    runs: list[str] = []
    kpis: list[list[dict]] = []
    bars: list[pd.DataFrame] = []
    tiers: list = []

    def _frame(latest: str, tib: float) -> QueryResult:
        return _ok(pd.DataFrame({"DATABASE_NAME": ["DB1"], "DB_BYTES": [tib * _TIB], "FAILSAFE_BYTES": [0.0],
                                 "DAYS_AVERAGED": [20.0], "LATEST_DAY": [latest]}))

    def _run(_sql, *_a, key: str = "", **_k):
        runs.append(key)
        if key.startswith(("storage_lastmonth_live_", "storage_prior_live_")):
            return _frame("2026-08-31", 1.0)
        if key.startswith(("storage_lastmonth_", "storage_prior_")):
            return _frame(fact_latest, fact_tib)
        if key.startswith("storage_mtd_"):
            return _frame("2026-09-29", 1.2)
        return _ok(pd.DataFrame())

    monkeypatch.setattr(spend, "account_today", lambda: today)
    monkeypatch.setattr(date_windows, "account_today", lambda: today)
    monkeypatch.setattr(spend, "run", _run)
    monkeypatch.setattr(spend, "kpi_row", lambda items, *a, **k: kpis.append(list(items)))
    monkeypatch.setattr(spend.charts, "bar_usd", lambda df, *a, **k: bars.append(df.copy()))
    monkeypatch.setattr(spend, "_storage_table_drill", lambda *a, **k: None)
    monkeypatch.setattr(spend, "_account_storage_tiers", lambda *a, bounds=None, **k: tiers.append(bounds))
    monkeypatch.setattr(spend, "_C07_STORAGE_ARGS", {"bounds": bounds}, raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _spend._storage_tab("ALL", 30, {"STORAGE_USD_PER_TB_MONTH": 23}, **_spend._C07_STORAGE_ARGS)

    at = AppTest.from_function(_app, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return runs, kpis, bars, tiers


def test_last_month_live_fallback_is_not_rescaled_by_the_stale_fact_watermark(monkeypatch):
    """R1-155: the fact stalled on Aug 20 sent the panel to the live leg (already a full-month average), but
    the rescale reused the FACT watermark: 1 TiB read as 1.55 TiB (and $35.65/mo against a $23 bar)."""
    runs, kpis, bars, _ = _storage(monkeypatch, bounds=(date(2026, 8, 1), date(2026, 9, 1)))
    assert runs[:2] == ["storage_lastmonth_ALL", "storage_lastmonth_live_ALL"]
    kpi = kpis[-1][0]
    assert kpi["label"] == "Storage last month (daily avg)"
    assert kpi["value"] == "1.00 TiB"
    assert float(bars[-1]["USD_MONTH"].sum()) == pytest.approx(23.0)
    assert "$23.00" in kpi["delta"]                                       # the KPI matches its own chart


def test_last_month_fact_with_a_short_tail_is_still_backfilled(monkeypatch):
    """The fact-served path keeps its watermark backfill (a 1-day tail gap: Aug 30 of 31)."""
    _runs, kpis, _bars, _ = _storage(monkeypatch, bounds=(date(2026, 8, 1), date(2026, 9, 1)),
                                     fact_latest="2026-08-30", fact_tib=30 / 31)
    assert kpis[-1][0]["value"] == "1.00 TiB"


@pytest.mark.parametrize("preset_bounds", [(date(2026, 9, 1), date(2026, 10, 1)),     # Current month
                                           (date(2026, 1, 1), date(2026, 10, 1))])    # Current year
def test_period_to_date_presets_show_mtd_storage_not_last_month(monkeypatch, preset_bounds):
    """R1-156: Current month / Current year carried bounds since r30 #2, so 'bounds is not None' sent both
    into the previous-month branch and the per-database panel never showed MTD."""
    runs, kpis, _bars, tiers = _storage(monkeypatch, bounds=preset_bounds)
    assert not any(r.startswith("storage_lastmonth_") for r in runs), runs
    assert runs[0] == "storage_mtd_ALL"
    assert kpis[-1][0]["label"] == "Storage MTD (daily avg)"
    assert tiers == [preset_bounds]                     # the tier panel still honours the selected window
