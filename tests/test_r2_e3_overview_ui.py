"""Round-2 bug hunt, cluster e3 (Overview / Brief / components / theme / Proof / Control Room), v4.608.

  R2-016 / R2-049  Overview's score de-cumulates queued time and remote spill by the days its window covered,
                   on the ACCOUNT clock the SQL window uses (was the UTC process clock: ~1.7x every evening).
  R2-029           a declare blocked by an already-open family incident names that incident and says the
                   proposal's alerts are NOT linked to it (was "its alerts stay linked there").
  R2-051           the stamps a viewer sees (QueryResult.fetched_at, the error ref and 'at', the telemetry
                   'at') are account time, not the UTC process clock.
  R2-059           the active-filter glow targets the keyed toolbar (the wrapper div it used is gone).
  R2-060           sparkline gradient ids are keyed on the gradient's color, so two cards with the same shape
                   and different colors never share one id.
  R2-072           guard() drops the caller's setup hint under a timeout / other failure.
  R2-075 / R2-085  Overview's cost-driver panel, monthly chart and runway bar say a failed read failed.
  R2-083 + lead    Brief's savings tile, spend spark, digest and runway tile no longer vanish silently.
  R2-084           Proof ▸ Pipeline never claims "no open actions" after the action-queue read failed.

AppTest renders use the shaped harness (its autouse stub fixture is imported below) and assert only
version-independent invariants (captured helper calls, not Streamlit element internals).
"""

from __future__ import annotations

import datetime as _dt
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402
from app.logic import formulas  # noqa: E402
from tests.test_pages_shaped import (  # noqa: E402, F401  (_stub_shaped: the harness's autouse stub fixture)
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_from_sql,
    _shaped_mart_first,
    _shaped_run,
    _stub_shaped,
)


def _failed(kind: str = "timeout") -> QueryResult:
    return QueryResult(ok=False, error=f"stub {kind}", error_kind=kind, source="stub")


def _freeze_clock(monkeypatch, utc_iso: str) -> None:
    """Freeze formulas' clock (account_now / account_today read it) at a UTC instant. A datetime subclass, so
    every other datetime use in formulas keeps working."""
    instant = datetime.fromisoformat(utc_iso).replace(tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(formulas, "datetime", _Frozen)


def _overrides(fail: dict, frames: dict):
    def _hit(key: str):
        for table in (fail, frames):
            for prefix, res in table.items():
                if key.startswith(prefix):
                    return res
        return None
    return _hit


# =============================================================================== Overview ====

def _render_overview(monkeypatch, *, fail: dict | None = None, frames: dict | None = None,
                     calendar_window: str | None = None) -> dict:
    from app.logic import scoring
    from app.ui.pages import overview as ov

    hit = _overrides(dict(fail or {}), dict(frames or {}))
    got: dict = {"empty": [], "signals": []}

    def _run(*a, **k):
        return hit(str(k.get("key", ""))) or _shaped_run(*a, **k)

    def _mart_first(*a, **k):
        return hit(str(k.get("key", ""))) or _shaped_mart_first(*a, **k)

    def _batch(specs, **k):
        out = _shaped_batch(specs, **k)
        for s in specs:
            res = hit(str(s.get("key", "")))
            if res is not None:
                out[s["key"]] = res
        return out

    real_empty, real_score = ov.empty_state, scoring.platform_score

    def _empty(kind, message="", *a, **k):
        got["empty"].append((kind, message, k.get("detail", "")))
        return real_empty(kind, message, *a, **k)

    def _score(*a, **k):
        got["signals"].append(dict(k.get("signals") or {}))
        return real_score(*a, **k)

    monkeypatch.setattr(ov, "run", _run)
    monkeypatch.setattr(ov, "run_mart_first", _mart_first)
    monkeypatch.setattr(ov, "run_batch", _batch)
    monkeypatch.setattr(ov, "empty_state", _empty)
    monkeypatch.setattr(ov, "org_balance_result", lambda _page: None)
    monkeypatch.setattr(scoring, "platform_score", _score)
    if calendar_window is not None:
        from app.logic.date_windows import resolve_window_days, window_bounds, window_scope_label

        real_filters = ov.filters

        def _filters():
            f = dict(real_filters())
            f.update(window=calendar_window, bounds=window_bounds(calendar_window),
                     window_label=window_scope_label(calendar_window),
                     days=resolve_window_days(calendar_window))
            return f

        monkeypatch.setattr(ov, "filters", _filters)
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Overview")
    at.run()
    assert not at.exception, at.exception
    return got


@pytest.mark.parametrize(("utc_iso", "covered_hours"), [
    ("2026-10-01 01:00", 44.0),    # 20:00 CDT: yesterday 00:00 CT .. now = 24 + 20 hours
    ("2026-09-30 17:00", 36.0),    # 12:00 CDT
    ("2026-09-30 05:30", 24.5),    # 00:30 CDT, just after Central midnight
    ("2026-12-02 00:00", 42.0),    # 18:00 CST (winter, UTC-6)
])
def test_score_divisor_covers_the_central_window(monkeypatch, utc_iso, covered_hours):
    """R2-016 / R2-049: the divisor is (24h + Central hours since midnight) / 24 -- the span the
    CURRENT_DATE()-anchored, Central-wall-clock HOUR_TS window actually covers."""
    from app.ui.pages import overview as ov

    _freeze_clock(monkeypatch, utc_iso)
    assert ov._score_window_elapsed_days(formulas.account_now()) == pytest.approx(covered_hours / 24.0)


def test_steady_queueing_reads_the_same_per_day_rate_in_the_central_evening(monkeypatch):
    """A steady 9 min/day of queueing and 4 GB/day of spill, summed over the Central window at 20:00 CDT
    (44 hours), reads 9 min/day and 4 GB/day -- under the 10-min / 5-GB thresholds, so no penalty. The UTC
    divisor read ~15.8 min/day and ~7 GB/day here and fired both drivers until Central midnight."""
    _freeze_clock(monkeypatch, "2026-10-01 01:00")
    span_days = 44.0 / 24.0
    window = QueryResult(ok=True, source="stub", df=pd.DataFrame({
        "QUERY_COUNT": [1000.0], "FAILED_COUNT": [0.0],
        "QUEUED_SEC": [9.0 * 60.0 * span_days], "SPILL_REMOTE_GB": [4.0 * span_days]}))
    got = _render_overview(monkeypatch, frames={"score_throughput_": window})
    sig = got["signals"][-1]
    assert sig["queue_minutes"] == pytest.approx(9.0)
    assert sig["spill_gb"] == pytest.approx(4.0)


def test_score_block_reads_no_utc_clock():
    from tests._source import read

    src = read("app/ui/pages/overview.py")
    assert "utcnow" not in src
    assert "_elapsed_days = _score_window_elapsed_days(account_now())" in src


def _drivers(got: dict) -> list[tuple]:
    return [e for e in got["empty"] if "driver" in str(e[1]).lower()]


@pytest.mark.parametrize("kind", ["timeout", "other", "missing_column"])
def test_failed_exec_board_read_is_unavailable_not_not_installed(monkeypatch, kind):
    """R2-075: the board read times out while FACT_WAREHOUSE_DAILY serves the trend -- the driver panel says the
    read failed, never that the mart 'appears once installed'."""
    got = _render_overview(monkeypatch, fail={"exec_board_": _failed(kind)})
    (kind_seen, msg, detail), = _drivers(got)
    assert kind_seen == "unavailable" and "couldn't be read" in msg and detail == f"stub {kind}"


def test_absent_exec_board_keeps_the_installed_wording(monkeypatch):
    got = _render_overview(monkeypatch, fail={"exec_board_": _failed("absent")})
    assert _drivers(got) == [("needs_setup", "Driver ranking appears once the exec board mart is installed.", "")]


def test_calendar_window_with_a_failed_spend_read_is_unavailable(monkeypatch):
    """R2-075: a calendar preset ranks drivers from the bounded warehouse frame; when that read fails the panel
    says so instead of 'No cost-driver rows'."""
    from app.config import LAST_MONTH_WINDOW

    got = _render_overview(monkeypatch, fail={"live_wh_daily_": _failed()}, calendar_window=LAST_MONTH_WINDOW)
    (kind_seen, msg, _detail), = _drivers(got)
    assert kind_seen == "unavailable" and "warehouse spend read failed" in msg


def test_failed_monthly_chart_read_is_unavailable(monkeypatch):
    """R2-085: both monthly legs fail -> an unavailable state (not a header over nothing)."""
    got = _render_overview(monkeypatch, fail={"ov_monthly_": _failed()})
    assert ("unavailable", "Monthly spend by warehouse unavailable.", "stub timeout") in got["empty"]


def test_empty_monthly_chart_read_says_no_rows_yet(monkeypatch):
    got = _render_overview(monkeypatch, fail={"ov_monthly_": QueryResult(ok=True, df=pd.DataFrame(),
                                                                         source="stub")})
    assert any(k == "no_data_yet" and m.startswith("No monthly warehouse spend") for k, m, _d in got["empty"])


def test_failed_runway_read_is_unavailable_and_unconfigured_stays_quiet(monkeypatch):
    """R2-085: a configured account whose contract read fails must not look unconfigured."""
    got = _render_overview(monkeypatch, fail={"ov_contract_runway": _failed()})
    assert any(k == "unavailable" and m.startswith("Contract runway unavailable") for k, m, _d in got["empty"])
    unconfigured = QueryResult(ok=True, source="stub", df=pd.DataFrame({
        "TOTAL": [0.0], "CONSUMED": [0.0], "DAILY_BURN": [1.0], "DAYS_LEFT": [None], "EXHAUST_DATE": [None]}))
    got = _render_overview(monkeypatch, frames={"ov_contract_runway": unconfigured})
    assert not [e for e in got["empty"] if "runway" in str(e[1]).lower()]


# ================================================================================== Brief ====

def _render_brief(monkeypatch, *, fail: dict | None = None, frames: dict | None = None,
                  spend: QueryResult | None = None) -> dict:
    from app.ui.pages import brief

    hit = _overrides(dict(fail or {}), dict(frames or {}))
    got: dict = {"empty": [], "kpis": [], "views": []}

    def _run(*a, **k):
        return hit(str(k.get("key", ""))) or _shaped_run(*a, **k)

    def _batch(specs, **k):
        out = _shaped_batch(specs, **k)
        for s in specs:
            res = hit(str(s.get("key", "")))
            if res is not None:
                out[s["key"]] = res
        return out

    real_empty, real_view = brief.empty_state, brief.ExecutiveSummaryView

    def _empty(kind, message="", *a, **k):
        got["empty"].append((kind, message, k.get("detail", "")))
        return real_empty(kind, message, *a, **k)

    def _view(**kw):
        got["views"].append(kw)
        return real_view(**kw)

    monkeypatch.setattr(brief, "run", _run)
    monkeypatch.setattr(brief, "run_batch", _batch)
    monkeypatch.setattr(brief, "empty_state", _empty)
    monkeypatch.setattr(brief, "kpi_row", lambda items, *a, **k: got["kpis"].extend(items))
    monkeypatch.setattr(brief, "ExecutiveSummaryView", _view)
    monkeypatch.setattr(brief, "org_balance_result", lambda _page: None)
    if spend is not None:
        monkeypatch.setattr(brief, "daily_spend_wide", lambda _page: spend)
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Brief")
    at.run()
    assert not at.exception, at.exception
    return got


def _tile(got: dict, label: str) -> dict | None:
    return next((k for k in got["kpis"] if k.get("label") == label), None)


def _card(got: dict, label: str) -> str | None:
    return dict(got["views"][-1]["cards"]).get(label)


@pytest.mark.parametrize(("kind", "delta"), [("timeout", "savings ledger unavailable"),
                                             ("absent", "savings ledger not installed")])
def test_failed_savings_read_keeps_a_dash_tile(monkeypatch, kind, delta):
    """R2-083: the savings tile is a dash with the reason, on screen and in the export -- never silently gone."""
    got = _render_brief(monkeypatch, fail={"roi": _failed(kind), "brief_roi": _failed(kind)})
    tile = _tile(got, "Verified savings run-rate")
    assert tile is not None and tile["value"] == "—" and tile["delta"] == delta
    assert _card(got, "Verified savings run-rate") == f"— | {delta}"


def test_failed_spend_spark_read_says_so(monkeypatch):
    got = _render_brief(monkeypatch, spend=_failed())
    assert ("unavailable", "Spend, 14 days unavailable — the daily metering read failed.", "stub timeout") \
        in got["empty"]
    notes = got["views"][-1]["scope_notes"]
    assert any("14-day spend trend could not be read" in n for n in notes)


def test_failed_digest_read_says_so_and_an_absent_one_stays_quiet(monkeypatch):
    got = _render_brief(monkeypatch, fail={"digest": _failed(), "daily_digest": _failed()})
    assert any(k == "unavailable" and m.startswith("AI morning narrative unavailable") for k, m, _d in got["empty"])
    got = _render_brief(monkeypatch, fail={"digest": _failed("absent"), "daily_digest": _failed("absent")})
    assert not [e for e in got["empty"] if "narrative" in str(e[1])]


def test_failed_runway_read_keeps_a_dash_tile(monkeypatch):
    """R2-085 (Brief twin): the credits read fails and no balance is readable -> a dash tile, not nothing."""
    got = _render_brief(monkeypatch, fail={"exh": _failed(), "brief_exhaustion": _failed()})
    tile = _tile(got, "Contract runway")
    assert tile is not None and tile["value"] == "—" and tile["delta"] == "runway read unavailable"


def test_overrun_contract_keeps_its_tile(monkeypatch):
    """lead (c07): an overrun credits runway (DAYS_LEFT < 0) used to drop the tile, so the export said nothing
    about the contract."""
    overrun = QueryResult(ok=True, source="stub", df=pd.DataFrame({
        "TOTAL": [1000.0], "CONSUMED": [1200.0], "DAILY_BURN": [10.0], "DAYS_LEFT": [-20.0],
        "EXHAUST_DATE": [_dt.date(2026, 9, 11)]}))
    got = _render_brief(monkeypatch, frames={"exh": overrun, "brief_exhaustion": overrun})
    tile = _tile(got, "Credit commitment exhausted")
    assert tile is not None and tile["value"] == "Exhausted" and tile["severity"] == "bad"
    assert _card(got, "Credit commitment exhausted") == "Exhausted | consumption is past the configured credits"


def test_unconfigured_contract_shows_no_runway_tile(monkeypatch):
    unconfigured = QueryResult(ok=True, source="stub", df=pd.DataFrame({
        "TOTAL": [0.0], "CONSUMED": [0.0], "DAILY_BURN": [1.0], "DAYS_LEFT": [None], "EXHAUST_DATE": [None]}))
    got = _render_brief(monkeypatch, frames={"exh": unconfigured, "brief_exhaustion": unconfigured})
    assert not [k for k in got["kpis"] if "ontract" in str(k.get("label")) or "ommitment" in str(k.get("label"))]


# ========================================================================= Proof ▸ Pipeline ====

def _pipeline(monkeypatch, failing: dict) -> list[tuple]:
    from app.ui import decision_studio as ds

    ds.reset_proof_memo()
    seen: list[tuple] = []
    monkeypatch.setattr(ds, "run", lambda sql, *_a, key="", **_k: failing.get(key) or _shaped_from_sql(sql))
    monkeypatch.setattr(ds, "run_batch", lambda specs, **_k: {
        s["key"]: failing.get(s["key"]) or _shaped_from_sql(s.get("sql", "")) for s in specs or []})
    monkeypatch.setattr(ds, "empty_state", lambda kind, msg, *_a, **_k: seen.append((kind, msg)))
    monkeypatch.setattr(ds, "kpi_row", lambda *_a, **_k: None)
    for name in ("hero_metric", "section_header", "result_caption", "styled_table", "selectable_nav_table"):
        monkeypatch.setattr(ds, name, lambda *_a, **_k: None)
    monkeypatch.setattr(ds, "can_open", lambda *_a, **_k: False)
    monkeypatch.setattr(ds, "st", SimpleNamespace(session_state={}, toggle=lambda *_a, **_k: False,
                                                  caption=lambda *_a, **_k: None, button=lambda *_a, **_k: False))
    monkeypatch.setattr(ds, "load_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(ds, "pipeline_frame", lambda *_a, **_k: pd.DataFrame())
    ds._pipeline_tab("ALL", 30, 3.68)
    ds.reset_proof_memo()
    return seen


def test_pipeline_never_claims_no_open_actions_after_a_failed_queue_read(monkeypatch):
    """R2-084: the queue read timed out -- the red line explains the empty pipeline; no quiet sentence then
    asserts 'no open actions' or sends the reader to create actions."""
    seen = _pipeline(monkeypatch, {"proof_queue_ALL": _failed()})
    assert any(k == "unavailable" and "action queue could not be read" in m for k, m in seen)
    assert not [m for _k, m in seen if "no open actions" in m or "Create actions" in m]


def test_pipeline_with_a_failed_idle_read_claims_only_what_was_read(monkeypatch):
    seen = _pipeline(monkeypatch, {"proof_idle_ALL_30": _failed()})
    msgs = [m for k, m in seen if k == "no_data_yet"]
    assert msgs and all("no addressable savings" not in m for m in msgs)
    assert any("from what could be read" in m and "no open actions" in m for m in msgs)


def test_pipeline_with_good_reads_keeps_the_full_sentence(monkeypatch):
    seen = _pipeline(monkeypatch, {})
    assert any(k == "no_data_yet" and "no addressable savings" in m and "no open actions" in m for k, m in seen)


# ========================================================================== Control Room ====

def test_family_open_message_names_the_incident_and_says_alerts_are_not_linked():
    """R2-029: a proposal holds only unlinked alerts and the no-op links none, so the message must not claim
    they 'stay linked there'."""
    from app.ui.pages import control_room as cr

    msg = cr._family_open_message("INC-42", 3)
    assert msg.startswith("No new incident — this family already has an open")    # round-15 lock prefix
    assert "open incident (INC-42)" in msg and "the 3 alerts in this proposal are NOT linked to it" in msg
    assert "stay linked" not in msg and "close it, then declare again" in msg
    assert "this proposal's alerts are NOT linked" in cr._family_open_message(None, None)
    assert "the 1 alert in this proposal is NOT linked to it" in cr._family_open_message("X", 1)


def test_family_open_check_returns_the_blocking_incident():
    import sqlglot

    from app.ui.pages import control_room as cr
    from tests._source import read

    chk = cr._incident_family_open_check_sql("ALFA", "COST_WH_DAILY_CREDITS|ALFA|WAREHOUSE|WH_X")
    assert "AS ALREADY_OPEN" in chk and "AS OPEN_INCIDENT_ID" in chk
    assert "MAX_BY(i.INCIDENT_ID, i.DETECTED_AT)" in chk
    # both legs carry the same family + entity predicate as the declare guard
    assert chk.count("SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = 'COST_WH_DAILY_CREDITS'") == 2
    assert chk.count("UPPER('WH_X')") == 2
    sqlglot.parse_one(chk, read="snowflake")
    src = read("app/ui/pages/control_room.py")
    assert "stay linked there" not in src and "link from the timeline drill or proposals" not in src
    assert '_family_open_message(_open_chk.df.iloc[0].get("OPEN_INCIDENT_ID"),' in src


# ============================================================================= components ====

def test_spark_gradient_ids_are_keyed_on_color_not_shape():
    """R2-060: two flat series (same normalized shape) in different colors used to share one gradient id."""
    from app.ui import palette
    from app.ui.components import spark_svg

    info, bad = spark_svg([3, 3, 3, 3], color=palette.INFO), spark_svg([2, 2, 2, 2], color=palette.BAD)
    (gi,), (gb,) = re.findall(r'linearGradient id="([^"]+)"', info), re.findall(r'linearGradient id="([^"]+)"', bad)
    assert gi != gb
    assert f'fill="url(#{gi})"' in info and f'fill="url(#{gb})"' in bad
    # deterministic: the same color always yields the same id (no per-process salted hash)
    assert re.findall(r'linearGradient id="([^"]+)"', spark_svg([1, 5, 2], color=palette.INFO)) == [gi]
    assert re.fullmatch(r"[A-Za-z0-9_-]+", gi)


@pytest.mark.parametrize(("kind", "shown"), [("timeout", False), ("other", False), ("", False),
                                             ("missing_column", True), ("unknown_function", True)])
def test_guard_keeps_the_setup_hint_only_for_absence_or_drift(monkeypatch, kind, shown):
    """R2-072: a timeout under 'Alerting is not installed yet' sent an admin to fix setup that was in place."""
    from app.ui import components

    seen: list[tuple] = []
    monkeypatch.setattr(components, "empty_state", lambda k, m, *a, **kw: seen.append((k, kw.get("hint", ""))))
    res = QueryResult(ok=False, error="The query hit its statement timeout.", error_kind=kind)
    assert components.guard(res, "", setup_hint="Alerting is not installed yet") is False
    assert seen == [("unavailable", "Alerting is not installed yet" if shown else "")]


def test_guard_needs_setup_branch_keeps_its_hint(monkeypatch):
    from app.ui import components

    seen: list[tuple] = []
    monkeypatch.setattr(components, "empty_state", lambda k, m, *a, **kw: seen.append((k, kw.get("hint", ""))))
    res = QueryResult(ok=False, error="Object X does not exist — run the migrations and roles.sql",
                      error_kind="absent")
    components.guard(res, "", setup_hint="apply V024")
    assert seen == [("needs_setup", "apply V024")]


# ================================================================================== theme ====

def test_active_filter_glow_targets_the_keyed_toolbar():
    """R2-059: Streamlit >= 1.52 has no stVerticalBlockBorderWrapper element; the bordered keyed container is
    one div with class st-key-ow_triage_toolbar, which carries the border."""
    from app import theme

    assert ".st-key-ow_triage_toolbar:has(.ow-scope-active){" in theme._CSS
    assert 'data-testid="stVerticalBlockBorderWrapper"' not in theme._CSS


# ======================================================================== account clock ====

def test_error_ref_and_buffer_stamp_are_account_time(monkeypatch):
    """R2-051: at 01:12:05 UTC on Oct 1 it is 20:12:05 on Sep 30 in Chicago, which is what APP_ERROR_LOG's
    LOGGED_AT holds -- the ref and the Admin 'at' column now agree with it."""
    from app.core import errors

    _freeze_clock(monkeypatch, "2026-10-01 01:12:05")
    import app.core.session as session_mod

    monkeypatch.setattr(session_mod, "get_cached_session", lambda: None)   # no off-box sink in a unit test
    st.session_state.clear()
    try:
        ref = errors.record_error("t", RuntimeError("boom"), context="unit")
        assert ref.startswith("OW-20260930-201205-")
        assert errors.error_buffer()[-1]["at"] == "2026-09-30T20:12:05"
    finally:
        st.session_state.clear()


def test_query_fetched_at_and_telemetry_are_account_time(monkeypatch):
    import app.core.query as q

    _freeze_clock(monkeypatch, "2026-10-01 01:12:05")
    st.session_state.clear()
    monkeypatch.setitem(q._FETCHERS, "live", lambda _sql, _scope, _page="": pd.DataFrame({"X": [1]}))
    try:
        res = q.run("SELECT 1 AS X /* R2-051 account clock */", page="t", key="r2_051", tier="live")
        assert res.ok and res.fetched_at == datetime(2026, 9, 30, 20, 12, 5)
        assert q.query_telemetry().iloc[-1]["at"] == "2026-09-30T20:12:05"
    finally:
        st.session_state.clear()
