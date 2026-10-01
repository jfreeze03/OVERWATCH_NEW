"""Alerts page: a FAILED read is never shown as setup, an absence or an all-clear (v4.605 rule).

Review R1-045 / R1-111 / R1-171 ('Assemble the evidence'), R1-116 / R1-172 (Rules + History panels), R1-168
(route backlog) and R1-169 (Native delivery task state). Before the fix a timeout, schema drift or a warehouse
error rendered as "Precision is not installed yet", "... appears once ...", "No evidence rows for this alert's
scope", "No route has an undelivered backlog right now" or "the notify task is suspended" -- each blaming setup,
the data or the task for a read that broke, with the error nowhere on the page. Now a failed read renders
needs_setup only for a true absence (is_setup_absence) and 'unavailable' + the error otherwise; the ok-and-empty
wording is reached only by a successful read.
"""

from __future__ import annotations

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402

_ERRORS = {
    "timeout": "The query hit its statement timeout. Narrow the window or filters and retry.",
    "missing_column": "SQL compilation error: invalid identifier 'RESOLUTION_KIND'",
    "other": "Warehouse 'WH_ALFA_ADMIN' cannot be resumed because resource monitor has exceeded its quota.",
    "absent": "The current role cannot access this object. If OVERWATCH setup is new, run the migrations and "
              "roles.sql.",
    "privilege": "The current role cannot access this object. If OVERWATCH setup is new, run the migrations and "
                 "roles.sql.",
}
_FAILED_KINDS = ("timeout", "missing_column", "other")
_ABSENT_KINDS = ("absent", "privilege")


def _fail(kind: str) -> QueryResult:
    return QueryResult(ok=False, error=_ERRORS[kind], error_kind=kind)


def _ok(df: pd.DataFrame | None = None) -> QueryResult:
    return QueryResult(df=pd.DataFrame() if df is None else df, ok=True)


# ------------------------------------------------------------------------------------- the shared helper ----

class _Rec:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, kind, message, **kw):
        self.calls.append((kind, message, kw.get("detail", "")))


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_failed_read_helper_is_unavailable_with_the_error(monkeypatch, kind):
    from app.ui.pages import alerts
    rec = _Rec()
    monkeypatch.setattr(alerts, "empty_state", rec)
    assert alerts._failed_read(_fail(kind), "X could not be read.") is True
    assert rec.calls == [("unavailable", "X could not be read.", _ERRORS[kind])]


@pytest.mark.parametrize("kind", _ABSENT_KINDS)
def test_failed_read_helper_is_needs_setup_only_for_a_true_absence(monkeypatch, kind):
    from app.ui.pages import alerts
    rec = _Rec()
    monkeypatch.setattr(alerts, "empty_state", rec)
    assert alerts._failed_read(_fail(kind), "X could not be read.", setup="X needs its table.") is True
    assert rec.calls == [("needs_setup", "X needs its table.", "")]


def test_failed_read_helper_leaves_an_ok_read_to_the_caller(monkeypatch):
    from app.ui.pages import alerts
    rec = _Rec()
    monkeypatch.setattr(alerts, "empty_state", rec)
    assert alerts._failed_read(_ok(), "X") is False and rec.calls == []


# ------------------------------------------------------------------- the Rules / History sections (AppTest) ----

_RULES = pd.DataFrame({"RULE_ID": ["COST_X"], "FAMILY": ["COST"], "NAME": ["x"], "ENABLED": [True],
                       "SEVERITY": ["HIGH"], "THRESHOLD_NUM": [30.0], "WINDOW_HOURS": [24], "OWNER": [""],
                       "CHANNEL": [""], "UPDATED_AT": [pd.Timestamp("2026-09-01")]})
_PREC = pd.DataFrame({"RULE_ID": ["COST_X"], "ACTIONED": [3], "NOISE": [1], "PRECISION_PCT": [75.0]})
_FAT = pd.DataFrame({"RULE_ID": ["COST_X"], "EVENTS": [9], "PER_WEEK": [2.1]})
_ROUTES_OK = pd.DataFrame({"ROUTE_ID": ["r1"], "INTEGRATION_NAME": ["OVERWATCH_TEAMS"], "BACKLOG": [0],
                           "OLDEST_MIN": [None]})
_SLO = pd.DataFrame({"UNDELIVERED_CRITICALS_30M": [0], "EXPIRED_UNDELIVERED": [0], "EVENTS_DELIVERED": [5],
                     "EVENTS_RAISED": [5], "MEDIAN_MIN": [1.0], "P95_MIN": [2.0], "ROUTE_FAILURES": [0]})

_SECTION_RESULTS: dict[str, QueryResult] = {}


def _stub(monkeypatch, results: dict[str, QueryResult]):
    """Route every alerts.run / run_batch read by its key; unknown keys read ok-and-empty."""
    from app.ui import schema_gate
    from app.ui.pages import alerts
    _SECTION_RESULTS.clear()
    _SECTION_RESULTS.update(results)

    def _run(*_a, **kw):
        return _SECTION_RESULTS.get(str(kw.get("key", "")), _ok())

    def _run_batch(specs, **_kw):
        return {s["key"]: _SECTION_RESULTS.get(s["key"], _ok()) for s in specs}

    monkeypatch.setattr(alerts, "run", _run)
    monkeypatch.setattr(alerts, "run_batch", _run_batch)
    monkeypatch.setattr(alerts, "has_migration", lambda *_a, **_k: False)
    monkeypatch.setattr(schema_gate, "has_migration", lambda *_a, **_k: False)
    import app.core.query as query_mod
    monkeypatch.setattr(query_mod, "execute_statement_async", lambda *a, **k: True)


def _render_section():
    from app.ui.pages import alerts
    alerts.render()


def _app(section: str) -> AppTest:
    at = AppTest.from_function(_render_section, default_timeout=20)
    at.session_state["alerts_section"] = section
    at.run()
    assert not at.exception, at.exception
    return at


def _texts(at: AppTest) -> dict[str, list[str]]:
    return {"error": [e.value for e in at.error], "info": [e.value for e in at.info],
            "caption": [c.value for c in at.caption], "warning": [w.value for w in at.warning]}


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_rules_failed_precision_is_unavailable_not_a_pending_migration(monkeypatch, kind):
    _stub(monkeypatch, {"alert_rules": _ok(_RULES), "rule_precision": _fail(kind)})
    t = _texts(_app("Rules"))
    assert "Rule precision could not be read." in t["error"]
    assert not any("not installed yet" in m or "pending schema update" in m for m in t["info"])


@pytest.mark.parametrize("kind", _ABSENT_KINDS)
def test_rules_absent_alert_events_is_setup(monkeypatch, kind):
    _stub(monkeypatch, {"alert_rules": _ok(_RULES), "rule_precision": _fail(kind)})
    t = _texts(_app("Rules"))
    assert any(m.startswith("Rule precision needs ALERT_EVENTS") for m in t["info"])
    assert "Rule precision could not be read." not in t["error"]


def test_rules_failed_sub_reads_are_unavailable_not_appears_once(monkeypatch):
    _stub(monkeypatch, {"alert_rules": _ok(_RULES), "rule_precision": _ok(_PREC),
                        "rule_metric_kinds": _fail("timeout"), "rule_prec_res:COST_X": _fail("other")})
    at = AppTest.from_function(_render_section, default_timeout=20)
    at.session_state["alerts_section"] = "Rules"
    at.session_state["rule_prec_sel_last"] = "COST_X"
    at.run()
    assert not at.exception, at.exception
    t = _texts(at)
    assert "Suggested thresholds could not be read." in t["error"]
    assert "Recent resolutions for COST_X could not be read." in t["error"]
    assert not any("Suggestions appear once" in c or "No resolved events yet" in c for c in t["caption"])


def test_rules_ok_but_empty_sub_reads_keep_their_wording(monkeypatch):
    _stub(monkeypatch, {"alert_rules": _ok(_RULES), "rule_precision": _ok(_PREC)})
    at = AppTest.from_function(_render_section, default_timeout=20)
    at.session_state["alerts_section"] = "Rules"
    at.session_state["rule_prec_sel_last"] = "COST_X"
    at.run()
    t = _texts(at)
    assert t["error"] == []
    assert any("Suggestions appear once" in c for c in t["caption"])
    assert "No resolved events yet for COST_X." in t["caption"]


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_history_failed_reads_are_unavailable(monkeypatch, kind):
    _stub(monkeypatch, {"mttr": _fail(kind), "inc": _fail(kind), "slo": _fail(kind), "fat": _fail(kind)})
    t = _texts(_app("History"))
    for sentence in ("MTTA / MTTR could not be read.", "Incident lifecycle metrics could not be read.",
                     "Delivery SLOs could not be read — undelivered criticals may be hidden.",
                     "Alert fatigue could not be read."):
        assert sentence in t["error"], sentence
    for wording in ("MTTA/MTTR appears once", "Incident lifecycle metrics appear once",
                    "Delivery SLOs appear once", "Fatigue metrics appear once"):
        assert not any(wording in c for c in t["caption"]), wording


def test_history_ok_but_empty_reads_keep_their_wording(monkeypatch):
    _stub(monkeypatch, {})
    t = _texts(_app("History"))
    assert t["error"] == []
    for wording in ("MTTA/MTTR appears once", "Incident lifecycle metrics appear once",
                    "Delivery SLOs appear once", "Fatigue metrics appear once"):
        assert any(wording in c for c in t["caption"]), wording


def test_history_failed_fatigue_drill_is_unavailable(monkeypatch):
    _stub(monkeypatch, {"fat": _ok(_FAT), "alert_fatigue_ev:COST_X": _fail("timeout")})
    at = AppTest.from_function(_render_section, default_timeout=20)
    at.session_state["alerts_section"] = "History"
    at.session_state["alert_fatigue_sel_last"] = "COST_X"
    at.run()
    t = _texts(at)
    assert "Recent events for COST_X could not be read." in t["error"]
    assert "No events in 90d for COST_X." not in t["caption"]


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_route_backlog_failed_read_is_never_the_all_clear(monkeypatch, kind):
    _stub(monkeypatch, {"slo": _ok(_SLO), "bl": _fail(kind), "rt": _fail(kind)})
    at = _app("History")
    t = _texts(at)
    assert "Route backlog unavailable — a stuck route may be hidden." in t["error"]
    assert "Deliveries by route could not be read." in t["error"]
    assert not any("No route has an undelivered backlog" in m for m in t["caption"])
    assert not any("No route has an undelivered backlog" in md.value for md in at.markdown)


def test_route_backlog_ok_reads_name_their_real_state(monkeypatch):
    # no rows = no ENABLED route (route_backlog LEFT JOINs events onto every enabled route)
    _stub(monkeypatch, {"slo": _ok(_SLO), "bl": _ok()})
    t = _texts(_app("History"))
    assert "No enabled alert route — nothing is queued for delivery." in t["info"]
    # every enabled route at BACKLOG 0: the verified all-clear (a green ok row, never on a failure)
    _stub(monkeypatch, {"slo": _ok(_SLO), "bl": _ok(_ROUTES_OK)})
    at = _app("History")
    assert any("No route has an undelivered backlog right now." in md.value for md in at.markdown)
    assert _texts(at)["error"] == []


def test_route_backlog_with_a_backlog_shows_the_table_not_the_all_clear(monkeypatch):
    waiting = _ROUTES_OK.assign(BACKLOG=[4], OLDEST_MIN=[95.0])
    _stub(monkeypatch, {"slo": _ok(_SLO), "bl": _ok(waiting)})
    at = _app("History")
    assert not any("No route has an undelivered backlog" in md.value for md in at.markdown)
    assert any("A rising OLDEST" in c for c in _texts(at)["caption"])


# --------------------------------------------------------------------------- Native delivery (R1-169) ----

class _St:
    """The slice of streamlit _delivery_status touches, recording each banner."""

    def __init__(self):
        self.out: list[tuple[str, str]] = []

    def __getattr__(self, name):
        if name in ("success", "warning", "error", "info", "caption"):
            return lambda msg, *a, **k: self.out.append((name, str(msg)))
        raise AttributeError(name)


def _delivery(monkeypatch, task: QueryResult) -> list[tuple[str, str]]:
    from app.ui.pages import alerts
    integ = _ok(pd.DataFrame({"name": ["OVERWATCH_TEAMS"]}))
    routes = _ok(pd.DataFrame({"N": ["OVERWATCH_TEAMS"]}))
    last = _ok(pd.DataFrame({"LAST_SEND": ["2026-09-30 01:00:00"]}))
    by_key = {"delivery_integ": integ, "delivery_task": task, "delivery_last": last,
              "delivery_routes_integ": routes}
    seen: dict[str, str] = {}

    def _run(*_a, **kw):
        seen[str(kw.get("key"))] = str(kw.get("tier"))
        return by_key[str(kw.get("key"))]

    fake = _St()
    monkeypatch.setattr(alerts, "run", _run)
    monkeypatch.setattr(alerts, "st", fake)
    alerts._delivery_status()
    # Review R1-169: the task state rides the 5-minute tier, never the 4 h metadata entry
    assert seen["delivery_task"] == "recent"
    return fake.out


def test_delivery_started_task_is_live(monkeypatch):
    out = _delivery(monkeypatch, _ok(pd.DataFrame({"name": ["TASK_ALERT_NOTIFY"], "state": ["started"]})))
    assert out[0][0] == "success" and out[0][1].startswith("Delivery LIVE")


def test_delivery_suspended_task_says_suspended(monkeypatch):
    out = _delivery(monkeypatch, _ok(pd.DataFrame({"name": ["TASK_ALERT_NOTIFY"], "state": ["suspended"]})))
    assert out == [("warning", out[0][1])] and "notify task is suspended" in out[0][1]


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_delivery_failed_task_read_is_unknown_not_suspended(monkeypatch, kind):
    out = _delivery(monkeypatch, _fail(kind))
    assert len(out) == 1 and out[0][0] == "warning"
    assert "unable to verify" in out[0][1] and "UNKNOWN" in out[0][1]
    assert "notify task is suspended" not in out[0][1]


def test_delivery_invisible_task_is_unknown_not_suspended(monkeypatch):
    for task in (_ok(), _ok(pd.DataFrame({"name": ["TASK_ALERT_NOTIFY"]}))):   # no row / no state column
        out = _delivery(monkeypatch, task)
        assert len(out) == 1 and out[0][0] == "warning"
        assert "NOT evidence it is suspended" in out[0][1]
        assert "notify task is suspended" not in out[0][1]


# ------------------------------------------------------- 'Assemble the evidence' (R1-045 / R1-111 / R1-171) ----

def _drawer():
    # AppTest runs this body as its own script: everything it needs is built here, not read from module globals
    import pandas as _pd

    from app.core.result import QueryResult as _QR
    from app.ui.pages import alerts
    event = _pd.DataFrame({
        "EVENT_ID": ["E1EVENT0"], "RULE_ID": ["PERF_FINGERPRINT_DRIFT"],
        "RAISED_AT": [_pd.Timestamp("2026-09-30 06:40:12")], "COMPANY": ["ALFA"], "SEVERITY": ["HIGH"],
        "TITLE": ["Query family p95 39.5s -> 170.7s: CALL TRXS_ABC_FRAMEWORK.GW_CDA_TO_STAGE1_JSON_LOAD..."],
        "DETAIL": ["drift"], "METRIC_VALUE": [4.3], "STATUS": ["OPEN"], "ACK_BY": [None], "ACK_AT": [None],
    })
    alerts._open_events_section(_QR(df=event, ok=True), True, "ALL")


def _assemble(monkeypatch, evidence: QueryResult) -> AppTest:
    _stub(monkeypatch, {"ai_ev_query_family_E1EVENT0": evidence})
    at = AppTest.from_function(_drawer, default_timeout=20)
    at.session_state["_ow_nav_context"] = {"event_id": "E1EVENT0"}
    at.run()
    assert not at.exception, at.exception
    btn = [b for b in at.button if b.label == "Assemble the evidence"]
    assert btn, "the drawer did not open on the deep-linked event"
    btn[0].click().run()
    assert not at.exception, at.exception
    return at


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_failed_evidence_read_is_unavailable_with_the_error(monkeypatch, kind):
    at = _assemble(monkeypatch, _fail(kind))
    t = _texts(at)
    assert ("Could not assemble the evidence for this alert, so the AI evaluation stays locked."
            in t["error"])
    assert any(_ERRORS[kind] in c.value for c in at.code)            # the error is one click away
    assert not any("No evidence rows" in c for c in t["caption"])
    assert "_ai_expl_prompt_E1EVENT0" not in at.session_state          # the AI stays locked


@pytest.mark.parametrize("kind", _ABSENT_KINDS)
def test_absent_evidence_source_is_setup(monkeypatch, kind):
    t = _texts(_assemble(monkeypatch, _fail(kind)))
    assert any(m.startswith("The evidence source for this alert isn't installed") for m in t["info"])
    assert not any("No evidence rows" in c for c in t["caption"])


def test_empty_evidence_read_keeps_no_evidence_rows(monkeypatch):
    at = _assemble(monkeypatch, _ok())
    t = _texts(at)
    assert any(c.startswith("No evidence rows for this alert's scope") for c in t["caption"])
    assert not any("Could not assemble the evidence" in e for e in t["error"])
