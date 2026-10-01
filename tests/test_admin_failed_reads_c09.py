"""Bug-hunt c09 R1-175: Admin > Migrations & freshness and Admin > Performance render a FAILED read by its kind.

A timeout or any other non-absence failure used to read as a setup gap ('Run V001 first', 'Needs migration
V021', 'appears after V027') or as a clean absence ('no slow fetch persisted', 'no matching error logged').
Now: needs_setup only for a true absence (app.core.result.is_setup_absence), empty_state('unavailable', ...,
detail=error) otherwise, and the ok-but-empty captions only for a read that succeeded. Fakes only (no session).
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.result import QueryResult

_FAILED_KINDS = ("timeout", "other", "missing_column")


def _failed(kind: str) -> QueryResult:
    return QueryResult(ok=False, error=f"boom ({kind})", error_kind=kind)


class _Rec:
    """Records empty_state calls and every st.caption / st.markdown text."""

    def __init__(self) -> None:
        self.empties: list[tuple[str, str, object]] = []
        self.texts: list[str] = []

    def empty(self, kind, msg, *_a, **k):
        self.empties.append((kind, msg, k.get("detail")))

    def text(self, *a, **_k):
        self.texts.append(str(a[0]) if a else "")


def _fake_st(rec: _Rec, **state) -> SimpleNamespace:
    return SimpleNamespace(
        caption=rec.text, markdown=rec.text, warning=rec.text,
        checkbox=lambda *_a, **_k: False, toggle=lambda *_a, **_k: True,
        column_config=SimpleNamespace(NumberColumn=lambda *_a, **_k: None, Column=lambda *_a, **_k: None),
        session_state=dict(state))


def _patch_admin(monkeypatch, results: dict, rec: _Rec, *, select=None, **state):
    from app.ui.pages import admin

    def _run(_sql, *, key, **_k):
        return results.get(key, QueryResult(ok=True))

    monkeypatch.setattr(admin, "run", _run)
    monkeypatch.setattr(admin, "empty_state", rec.empty)
    for name in ("panel_help", "styled_table", "section_header", "result_caption", "kpi_row",
                 "_task_health_panel", "_stmt_timeout_ceiling", "_usage_detail_panels"):
        monkeypatch.setattr(admin, name, lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "_fresh_applied_versions", lambda *_a, **_k: set())
    monkeypatch.setattr(admin, "selectable_table", lambda *_a, **_k: select)
    monkeypatch.setattr(admin, "query_telemetry", lambda: pd.DataFrame())
    monkeypatch.setattr(admin, "st", _fake_st(rec, **state))
    return admin


# --------------------------------------------------------------------------- Migrations: SCHEMA_VERSION ----

@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_unreadable_schema_version_is_not_run_v001(monkeypatch, kind):
    rec = _Rec()
    admin = _patch_admin(monkeypatch, {"schema_version": _failed(kind)}, rec)
    admin._migrations_tab()
    assert [(k, m) for k, m, _ in rec.empties] == [("unavailable", "Cannot read SCHEMA_VERSION.")]


@pytest.mark.parametrize(("kind", "advice"), [("absent", "V001__core.sql"), ("privilege", "roles.sql")])
def test_absent_schema_version_still_gets_setup_advice(monkeypatch, kind, advice):
    rec = _Rec()
    admin = _patch_admin(monkeypatch, {"schema_version": _failed(kind)}, rec)
    admin._migrations_tab()
    assert [k for k, _m, _ in rec.empties] == ["unavailable", "needs_setup"]
    assert advice in rec.empties[1][1]


# --------------------------------------------------------------------------- Migrations: stale diagnosis ----

def test_unreadable_error_log_is_not_no_matching_error(monkeypatch):
    rec = _Rec()
    fresh = QueryResult(ok=True, df=pd.DataFrame({"SOURCE_NAME": ["MART_QH_HOURLY"], "HOURS_SINCE_LOAD": [50.0],
                                                  "ROW_COUNT": [10]}))
    admin = _patch_admin(monkeypatch, {"schema_version": QueryResult(ok=True, df=pd.DataFrame({"VERSION": [1]})),
                                       "adm_stale_errs": _failed("timeout")}, rec)
    monkeypatch.setattr(admin, "run_mart_first", lambda *_a, **_k: fresh)
    monkeypatch.setattr(admin, "guard", lambda res, *_a, **_k: res.usable())
    monkeypatch.setattr(admin, "_EXPECTED_MIGRATIONS", {1: "core"})
    admin._migrations_tab()
    assert ("unavailable", "APP_ERROR_LOG could not be read, so loader errors are not matched to the stale "
            "sources below.", "boom (timeout)") in rec.empties
    line = next(t for t in rec.texts if "MART_QH_HOURLY" in t)
    assert "error log unreadable" in line and "no matching error logged" not in line


# --------------------------------------------------------------------------- Performance: fleet + telemetry ----

def _render_performance(monkeypatch, results: dict, rec: _Rec, **state):
    admin = _patch_admin(monkeypatch, results, rec, **state)
    monkeypatch.setattr(admin, "guard", lambda res, *_a, **_k: res.usable())
    admin._performance_tab()


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_failed_fleet_reads_are_unavailable_not_needs_migration(monkeypatch, kind):
    rec = _Rec()
    _render_performance(monkeypatch, {"fleet_qstats": _failed(kind), "tel_by_page": _failed(kind)}, rec)
    assert ("unavailable", "Fleet fetch telemetry (APP_QUERY_TELEMETRY) could not be read.",
            f"boom ({kind})") in rec.empties
    assert ("unavailable", "Fleet telemetry by page could not be read.", f"boom ({kind})") in rec.empties
    assert not any(k == "needs_setup" and ("V021" in m or "V027" in m) for k, m, _ in rec.empties)
    assert not any("appears after V027" in t for t in rec.texts)


def test_absent_fleet_table_still_needs_setup(monkeypatch):
    rec = _Rec()
    _render_performance(monkeypatch, {"fleet_qstats": _failed("absent"), "tel_by_page": _failed("absent")}, rec)
    setup = [m for k, m, _ in rec.empties if k == "needs_setup"]
    assert any("V021" in m for m in setup) and any("V027" in m for m in setup)
    assert not any(k == "unavailable" and "telemetry" in m.lower() for k, m, _ in rec.empties)


def test_empty_per_page_telemetry_keeps_the_v027_caption(monkeypatch):
    rec = _Rec()
    _render_performance(monkeypatch, {}, rec)
    assert any("Per-page telemetry appears after V027" in t for t in rec.texts)


# --------------------------------------------------------------------------- Performance: the two drills ----

def test_failed_pain_drill_read_is_unavailable_not_nothing_persisted(monkeypatch):
    rec = _Rec()
    tbp = pd.DataFrame({"PAGE": ["Spend"], "EST_WAIT_S": [120.0], "P95_S": [3.1], "SLOW_2S": [4], "FAILED": [0]})
    admin = _patch_admin(monkeypatch, {"tel_by_page": QueryResult(ok=True, df=tbp),
                                       "fleet_qstats_pg:Spend": _failed("timeout")}, rec, select=0)
    admin._perf_rider_panels(None)
    assert ("unavailable", "Slow keys for Spend could not be read.", "boom (timeout)") in rec.empties
    assert not any("no slow (≥2s) or failed fetch persisted" in t for t in rec.texts)


def test_failed_slo_drill_read_is_unavailable_not_nothing_persisted(monkeypatch):
    rec = _Rec()
    slo = pd.DataFrame({"PAGE": ["Spend"], "SLO_STATE": ["FAIL"]})
    _render_performance(monkeypatch, {"app_perf_slo": QueryResult(ok=True, df=slo),
                                      "perf_slo_keys:Spend": _failed("timeout")}, rec,
                        perf_slo_sel_last="Spend")
    assert ("unavailable", "Slow keys for Spend could not be read.", "boom (timeout)") in rec.empties
    assert not any("No slow (≥2s) or failed fetch persisted for Spend" in t for t in rec.texts)
