"""v4.608 round-2 fixes in the Entity 360 / Watchlist workbench (cluster e6).

R2-055: the Entity 360 metric snapshot follows the page Window -- Current month's day-0 offset on the 1st is
today only (it was a trailing 30 days via ``int(days or 30)``), and a calendar preset reads its real range
(Last month = the previous calendar month, not a trailing span ending today).
R2-078: a failed metrics / evidence / catalog-browse read renders by its kind (unavailable with the error, or
needs_setup for a true absence) -- never a silently missing block or the quiet "choose an entity" prompt.
R2-074: a watched warehouse whose cost or health read failed is never 'steady' on the Brief badge or the
Watchlist; the surfaces say what could not be checked. Review fix: the same for a task / query-family watch whose
signals read failed, and 'steady' counts only evaluated watches (a type with no automatic check is named as such).

Rendered with the tests/test_workbench_failed_reads.py fakes (no SQL runs).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from app.config import CURRENT_MONTH_WINDOW
from app.logic.date_windows import CalendarDayOffset, resolve_window_days, window_bounds
from app.logic.watch_monitor import watched_status
from tests.test_probe_absence_split import _FAILED, _SETUP, _failed, _ok
from tests.test_workbench_failed_reads import _patch_page

sqlglot = pytest.importorskip("sqlglot")

_WH = "WH_A"
_METRICS_KEY = "entity_metrics_WAREHOUSE_WH_A"


def _capture_snapshot(monkeypatch) -> list[tuple]:
    """Record every entity_metric_snapshot call the render makes (args + kwargs), delegating to the builder."""
    from app.data import workbench_sql
    real = workbench_sql.entity_metric_snapshot
    calls: list[tuple] = []

    def spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(workbench_sql, "entity_metric_snapshot", spy)
    return calls


def _sql_by_key(monkeypatch, wb, results: dict) -> dict[str, str]:
    """Re-patch run() so it also records the SQL each key was read with."""
    sqls: dict[str, str] = {}

    def fake_run(sql, *_a, key: str = "", **_k):
        sqls[key] = sql
        return results.get(key, _ok(pd.DataFrame()))

    monkeypatch.setattr(wb, "run", fake_run)
    return sqls


# ------------------------------------------------------------------------------ R2-055 ----

def test_current_month_day_zero_reads_today_not_a_trailing_30_days(monkeypatch):
    wb, _fake, _seen = _patch_page(monkeypatch, {}, entity_key=_WH)
    day0 = CalendarDayOffset(0)                                   # the 1st of the month
    today = date(2026, 10, 1)
    bounds = (today, today + timedelta(days=1))
    monkeypatch.setattr(wb, "filters", lambda: {"days": day0, "bounds": bounds})
    calls = _capture_snapshot(monkeypatch)
    sqls = _sql_by_key(monkeypatch, wb, {})
    wb.render_entity_360("ALL")
    ((args, kwargs),) = calls
    days = args[2]
    # the marker survives (no int(), no `or 30`) and the calendar bounds ride along
    assert int(days) == 0 and getattr(days, "calendar_window", False)
    assert kwargs.get("bounds") == bounds
    (sql,) = [s for k, s in sqls.items() if k.startswith(_METRICS_KEY)]
    assert "DAY >= '2026-10-01' AND DAY < '2026-10-02'" in sql
    assert "DATEADD('day', -30" not in sql


def test_last_month_reads_the_previous_calendar_month_not_a_trailing_span(monkeypatch):
    wb, _fake, _seen = _patch_page(monkeypatch, {}, entity_key=_WH)
    bounds = (date(2026, 9, 1), date(2026, 10, 1))
    monkeypatch.setattr(wb, "filters", lambda: {"days": CalendarDayOffset(30), "bounds": bounds})
    sqls = _sql_by_key(monkeypatch, wb, {})
    wb.render_entity_360("ALL")
    (sql,) = [s for k, s in sqls.items() if k.startswith(_METRICS_KEY)]
    assert sql.count("DAY >= '2026-09-01' AND DAY < '2026-10-01'") == 5        # every arm of the snapshot
    assert "CURRENT_DATE()" not in sql


def test_calendar_presets_get_their_own_cache_key_and_caption(monkeypatch):
    wb, fake, seen = _patch_page(monkeypatch, {}, entity_key=_WH)
    bounds = window_bounds(CURRENT_MONTH_WINDOW)
    days = resolve_window_days(CURRENT_MONTH_WINDOW)
    row = pd.DataFrame([{"METRIC": "Credits", "VALUE": 2.0, "UNIT": "credits", "BASIS": "METERED",
                         "AS_OF": "2026-10-01"}])
    monkeypatch.setattr(wb, "filters", lambda: {"days": days, "bounds": bounds})
    sqls = _sql_by_key(monkeypatch, wb, {f"{_METRICS_KEY}_{days}_{bounds[0]}_{bounds[1]}": _ok(row)})
    wb.render_entity_360("ALL")
    assert f"{_METRICS_KEY}_{days}_{bounds[0]}_{bounds[1]}" in sqls
    assert seen["kpis"], "the metric cards render"
    caption = fake.text("caption")
    assert "Entity evidence for the current month" in caption and "-day entity evidence" not in caption


def test_trailing_windows_keep_the_builder_byte_identical():
    from app.data import workbench_sql
    for kind in workbench_sql.ENTITY_METRIC_TYPES:
        trailing = workbench_sql.entity_metric_snapshot(kind, "O'Brien", 30)
        assert trailing == workbench_sql.entity_metric_snapshot(kind, "O'Brien", 30, bounds=None)
        assert "DAY >= DATEADD('day', -30, CURRENT_DATE())" in trailing
        # a day-0 calendar offset is today only, never widened into yesterday
        assert "DATEADD('day', -0, CURRENT_DATE())" in workbench_sql.entity_metric_snapshot(
            kind, "K", CalendarDayOffset(0))


@pytest.mark.parametrize("kind", ["WAREHOUSE", "DATABASE", "OBJECT", "TASK", "QUERY_FINGERPRINT", "USER", "ROLE"])
def test_bounded_snapshot_parses_and_never_mixes_in_a_trailing_predicate(kind):
    from app.data import workbench_sql
    sql = workbench_sql.entity_metric_snapshot(kind, "O'Brien", 30, bounds=(date(2026, 9, 1), date(2026, 10, 1)))
    assert "DAY >= '2026-09-01' AND DAY < '2026-10-01'" in sql and "DATEADD(" not in sql
    sqlglot.parse(sql, dialect="snowflake")


# ------------------------------------------------------------------------------ R2-078 ----

@pytest.mark.parametrize("kind", _FAILED)
def test_failed_metrics_read_is_unavailable_not_a_vanished_block(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {f"{_METRICS_KEY}_30": _failed(kind)}, entity_key=_WH)
    wb.render_entity_360("ALL")
    assert ("unavailable", "Entity metrics could not be read for this window.") in seen["empty"]
    assert f"boom ({kind})" in seen["detail"]
    assert ("no_data_yet", "No measured entity metrics exist in this window.") not in seen["empty"]
    assert not seen["kpis"]


@pytest.mark.parametrize("kind", _SETUP)
def test_absent_metric_mart_is_setup(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {f"{_METRICS_KEY}_30": _failed(kind)}, entity_key=_WH)
    wb.render_entity_360("ALL")
    assert ("needs_setup", "The warehouse metric mart is not installed or not readable yet.") in seen["empty"]
    assert not [m for s, m in seen["empty"] if s == "unavailable" and "metrics" in m]


def _evidence(monkeypatch, results: dict):
    wb, _fake, seen = _patch_page(monkeypatch, results, entity_key=_WH)
    monkeypatch.setattr(wb, "evidence_gate", lambda *_a, **_k: True)
    wb.render_entity_360("ALL")
    return seen


@pytest.mark.parametrize("kind", _FAILED)
def test_one_failed_evidence_read_beside_empty_ones_is_not_silent(monkeypatch, kind):
    seen = _evidence(monkeypatch, {"entity_savings_WAREHOUSE_WH_A": _failed(kind)})
    assert ("unavailable", "Savings outcomes (SAVINGS_LEDGER) could not be read for this entity.") in seen["empty"]
    assert f"boom ({kind})" in seen["detail"]
    # the all-empty claim needs all three reads to have succeeded
    assert ("no_data_yet", "No evidence, remediation, or savings outcome is linked yet.") not in seen["empty"]


def test_failed_evidence_read_beside_a_filled_one_is_named(monkeypatch):
    rem = pd.DataFrame([{"EXECUTED_AT": "2026-09-30", "FINDING_TYPE": "AUTO_SUSPEND"}])
    seen = _evidence(monkeypatch, {"entity_remed_WAREHOUSE_WH_A": _ok(rem),
                                   "entity_links_WAREHOUSE_WH_A": _failed("timeout")})
    assert any(t is not None and not t.empty for t in seen["tables"])            # remediation history shows
    assert ("unavailable", "Evidence relationships (EVIDENCE_LINKS) could not be read for this entity.") \
        in seen["empty"]


@pytest.mark.parametrize("kind", _SETUP)
def test_absent_evidence_table_is_setup(monkeypatch, kind):
    seen = _evidence(monkeypatch, {"entity_remed_WAREHOUSE_WH_A": _failed(kind)})
    assert ("needs_setup", "Remediation history (REMEDIATION_LOG) is not installed or not readable yet.") \
        in seen["empty"]


def test_all_empty_evidence_keeps_the_no_evidence_state(monkeypatch):
    seen = _evidence(monkeypatch, {})
    assert ("no_data_yet", "No evidence, remediation, or savings outcome is linked yet.") in seen["empty"]
    assert not [s for s, _m in seen["empty"] if s == "unavailable"]


@pytest.mark.parametrize("kind", _FAILED)
def test_failed_catalog_browse_is_unavailable_not_the_choose_prompt(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {"entity_catalog_browse": _failed(kind)})
    wb.render_entity_360("ALL")
    assert seen["empty"] == [("unavailable", "The entity catalog could not be read.")]
    assert seen["detail"] == [f"boom ({kind})"]


@pytest.mark.parametrize("kind", _SETUP)
def test_absent_catalog_browse_is_setup(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {"entity_catalog_browse": _failed(kind)})
    wb.render_entity_360("ALL")
    assert seen["empty"] == [("needs_setup", "V074 is required for the ownership catalog and watchlists.")]


def test_empty_catalog_browse_keeps_the_choose_prompt(monkeypatch):
    wb, _fake, seen = _patch_page(monkeypatch, {})
    wb.render_entity_360("ALL")
    assert seen["empty"] == [("no_data_yet",
                              "Choose an entity or open one from an action, table, or universal search.")]


# ------------------------------------------------------------------------------ R2-074 ----

def _wl(*keys: str) -> pd.DataFrame:
    return pd.DataFrame([{"ENTITY_TYPE": "WAREHOUSE", "ENTITY_KEY": k, "LABEL": k} for k in keys])


def test_unevaluated_warehouse_watch_is_never_steady():
    out = watched_status(_wl("WH_ETL", "WH_BI"), None, None)
    assert not out["STATUS"].eq("steady").any()
    assert out["STATUS"].str.contains("not checked").all()
    assert not out["ATTENTION"].any()                       # not a fabricated move either


def test_one_failed_arm_names_what_was_not_checked():
    healthy = pd.DataFrame([{"WAREHOUSE_NAME": "WH_ETL", "GRADE": "Healthy"}])
    row = watched_status(_wl("WH_ETL"), None, healthy).iloc[0]
    assert row["STATUS"] != "steady" and "spend not checked" in row["STATUS"]
    sick = pd.DataFrame([{"WAREHOUSE_NAME": "WH_ETL", "GRADE": "Degraded"}])
    flagged = watched_status(_wl("WH_ETL"), None, sick).iloc[0]
    assert bool(flagged["ATTENTION"]) and flagged["SEVERITY"] == "warn"
    assert "health: Degraded" in flagged["STATUS"] and "spend not checked" in flagged["STATUS"]


def test_evaluated_but_empty_reads_are_still_steady():
    out = watched_status(_wl("WH_ETL"), pd.DataFrame(), pd.DataFrame())
    assert out.iloc[0]["STATUS"] == "steady"


def _watch_page(monkeypatch, *, cost, health):
    from app.ui import workbench as wb
    from tests.test_probe_absence_split import _FakeSt
    fake = _FakeSt()
    seen: dict = {"empty": [], "detail": []}
    results = {"watch_auto_list": _ok(_wl("WH_ETL", "WH_BI")), "watch_auto_cost": cost, "watch_auto_health": health}

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    monkeypatch.setattr(wb, "st", fake)
    monkeypatch.setattr(wb, "run", lambda _sql, *_a, key="", **_k: results[key])
    monkeypatch.setattr(wb, "empty_state", fake_empty)
    monkeypatch.setattr(wb, "load_settings", lambda *_a, **_k: {})
    return wb, fake, seen


def test_brief_badge_never_calls_failed_reads_steady(monkeypatch):
    wb, fake, seen = _watch_page(monkeypatch, cost=_failed("timeout"), health=_failed("timeout"))
    wb.render_watch_badge("JOE", 3.68)
    assert "steady" not in fake.text("caption")
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "Could not fully check 2 watched warehouses" in msg
    assert "FACT_WAREHOUSE_DAILY: boom (timeout)" in seen["detail"][0]
    assert "MART_WAREHOUSE_EFFICIENCY_DAILY: boom (timeout)" in seen["detail"][0]


def test_brief_badge_steady_only_when_both_reads_were_evaluated(monkeypatch):
    wb, fake, seen = _watch_page(monkeypatch, cost=_ok(pd.DataFrame()), health=_ok(pd.DataFrame()))
    wb.render_watch_badge("JOE", 3.68)
    assert "2 watched entities are steady." in fake.text("caption")
    assert seen["empty"] == []


def test_watched_attention_marks_a_failed_cost_read_as_not_evaluated(monkeypatch):
    wb, _fake, _seen = _watch_page(monkeypatch, cost=_failed("other"), health=_ok(pd.DataFrame()))
    status = wb.watched_attention("JOE", 3.68)
    assert status["STATUS"].str.contains("spend not checked").all()
    assert status.attrs["read_errors"] == "FACT_WAREHOUSE_DAILY: boom (other)"


# ----------------------------------------------- R2-074 review fix: the task / query-family signals read ----

def _signal_page(monkeypatch, watches: list[tuple[str, str]], *, signals=None, cost=None):
    """Brief / Watchlist fakes over a mixed watchlist: health reads ok-empty (evaluated), the cost read returns
    ``cost`` and the task / query-family signals read ``signals`` (each default: ok-empty)."""
    from app.ui import workbench as wb
    from tests.test_probe_absence_split import _FakeSt
    fake = _FakeSt()
    seen: dict = {"empty": [], "detail": [], "runs": []}
    wl = pd.DataFrame([{"ENTITY_TYPE": t, "ENTITY_KEY": k, "LABEL": k} for t, k in watches])
    results = {"watch_auto_list": _ok(wl), "watchlist_all": _ok(wl),
               "watch_auto_cost": cost if cost is not None else _ok(pd.DataFrame()),
               "watch_auto_health": _ok(pd.DataFrame()), "watchlist_slo": _ok(pd.DataFrame()),
               "watch_auto_signals": signals if signals is not None else _ok(pd.DataFrame())}

    def fake_run(_sql, *_a, key: str = "", **_k):
        seen["runs"].append(key)
        return results[key]

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    monkeypatch.setattr(wb, "st", fake)
    monkeypatch.setattr(wb, "run", fake_run)
    monkeypatch.setattr(wb, "empty_state", fake_empty)
    monkeypatch.setattr(wb, "load_settings", lambda *_a, **_k: {})
    return wb, fake, seen


@pytest.mark.parametrize("etype", ["TASK", "QUERY_FINGERPRINT"])
def test_failed_signals_read_never_yields_a_steady_brief_badge(monkeypatch, etype):
    """Review fix: a failed watch_auto_signals read left the task / family row blank, which watch_unchecked did not
    count, so the badge printed '★ 1 watched entity is steady.' over a read that never ran."""
    wb, fake, seen = _signal_page(monkeypatch, [(etype, "DB.S.T1")], signals=_failed("timeout"))
    wb.render_watch_badge("JOE", 3.68)
    assert "steady" not in fake.text("caption")
    ((state, msg),) = seen["empty"]
    assert state == "unavailable"
    assert msg == ("Could not fully check 1 watched entity: the task / query-family signals read failed, so "
                   "STATUS names what was not checked.")
    assert seen["detail"] == ["MART_TASK_NODE_DAILY + MART_QUERY_FAMILY_DAILY + MART_PATTERN_COST_DAILY: "
                              "boom (timeout)"]
    status = wb.watched_attention("JOE", 3.68)
    assert status["STATUS"].str.contains("not checked").all() and not status["ATTENTION"].any()


def test_failed_signals_read_beside_a_failed_cost_read_names_both(monkeypatch):
    wb, _fake, seen = _signal_page(monkeypatch, [("WAREHOUSE", "WH_ETL"), ("TASK", "DB.S.T1")],
                                   signals=_failed("other"), cost=_failed("timeout"))
    wb.render_watch_badge("JOE", 3.68)
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and msg.startswith("Could not fully check 2 watched entities: a cost or health "
                                                     "read failed and the task / query-family signals read failed")
    assert "FACT_WAREHOUSE_DAILY: boom (timeout)" in seen["detail"][0]
    assert "MART_PATTERN_COST_DAILY: boom (other)" in seen["detail"][0]


def test_watchlist_tab_shows_the_unavailable_notice_for_a_failed_signals_read(monkeypatch):
    wb, _fake, seen = _signal_page(monkeypatch, [("TASK", "DB.S.T1")], signals=_failed("timeout"))
    tables: list[pd.DataFrame] = []
    monkeypatch.setattr(wb, "viewer_name", lambda: "JOE")
    monkeypatch.setattr(wb, "guard", lambda res, *_a, **_k: res.usable())
    monkeypatch.setattr(wb, "selectable_table", lambda df, *_a, **_k: tables.append(df))
    wb.render_watchlist()
    assert [s for s, _m in seen["empty"]] == ["unavailable"]
    assert "signals read failed" in seen["empty"][0][1]
    (table,) = tables
    assert table["STATUS"].tolist() == ["not checked (the task signals read failed)"]


def test_a_watch_with_no_automatic_check_is_never_counted_steady(monkeypatch):
    """A DATABASE watch (no proactive arm: STATUS blank) is pinned, never evaluated -- the badge does not call it
    steady."""
    wb, fake, seen = _signal_page(monkeypatch, [("DATABASE", "DB1")])
    wb.render_watch_badge("JOE", 3.68)
    assert fake.text("caption") == "★ 1 watched entity has no automatic check."
    assert seen["empty"] == [] and "watch_auto_signals" not in seen["runs"]


def test_steady_counts_only_the_evaluated_watches(monkeypatch):
    signals = _ok(pd.DataFrame(columns=["ENTITY_TYPE", "ENTITY_KEY_U", "DAY"]))
    wb, fake, _seen = _signal_page(monkeypatch, [("WAREHOUSE", "WH_ETL"), ("TASK", "DB.S.T1"),
                                                 ("DATABASE", "DB1"), ("USER", "ANA")], signals=signals)
    wb.render_watch_badge("JOE", 3.68)
    # the warehouse ('steady') and the task ('no runs in the last 30 days') were evaluated; the other two were not
    assert fake.text("caption") == "★ 2 watched entities are steady; 2 more entities have no automatic check."


def test_signals_failed_marks_only_the_watches_the_read_covered():
    from app.logic.watch_monitor import watch_steady, watch_unchecked, watch_unchecked_types
    wl = pd.DataFrame([{"ENTITY_TYPE": t, "ENTITY_KEY": k, "LABEL": k}
                       for t, k in (("TASK", "DB.S.T1"), ("QUERY_FINGERPRINT", "abc"), ("TASK", "DB.S.CAPPED"),
                                    ("WAREHOUSE", "WH_ETL"))])
    out = watched_status(wl, pd.DataFrame(), pd.DataFrame(), signals_failed=True,
                         signal_keys=[("TASK", "db.s.t1"), ("QUERY_FINGERPRINT", "ABC")]).set_index("ENTITY_KEY")
    assert out.loc["DB.S.T1", "STATUS"] == "not checked (the task signals read failed)"
    assert out.loc["abc", "STATUS"] == "not checked (the query-family signals read failed)"
    assert out.loc["DB.S.CAPPED", "STATUS"] == ""            # past the signals cap: not read, still blank
    assert out.loc["WH_ETL", "STATUS"] == "steady"
    assert not out["ATTENTION"].any()
    frame = out.reset_index()
    assert watch_unchecked(frame) == 2 and watch_unchecked_types(frame) == {"TASK", "QUERY_FINGERPRINT"}
    assert watch_steady(frame) == 1
    # without the flag (the read never ran) nothing changes: the rows stay blank
    quiet = watched_status(wl, pd.DataFrame(), pd.DataFrame())
    assert quiet.loc[quiet["ENTITY_TYPE"] != "WAREHOUSE", "STATUS"].eq("").all()
