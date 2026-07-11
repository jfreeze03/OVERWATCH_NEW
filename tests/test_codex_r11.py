"""Codex r11 fix-first batch — behavioral tests (r11 #14: exercise the
failure paths with fakes instead of only source-text locks).

Covers: submission failure marks only the confirmed failer (#4), pending
members never quarantined + rehab on clean solo run (#5), mart_accept
coverage gate (#2), identity-gated prefs attempts (#3), declarative canary
gaps (#7), environment-filter honesty (#1)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import app.core.query as q
from app.core.result import QueryResult

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeJob:
    def __init__(self, df=None, exc=None):
        self._df, self._exc = df, exc

    def result(self):
        if self._exc:
            raise self._exc
        return self._df


class _FakeStmt:
    def __init__(self, outcome):
        self._outcome = outcome

    def to_pandas(self, block=True):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeSession:
    """outcomes[i] drives sqls[i]: a _FakeJob submits fine; an Exception
    raises AT SUBMISSION (the r11 #4 path)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self._i = 0

    def sql(self, _sql):
        out = self._outcomes[self._i]
        self._i += 1
        return _FakeStmt(out)


@pytest.fixture
def _quiet(monkeypatch):
    monkeypatch.setattr(q, "get_session", lambda: None)
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q, "apply_statement_timeout", lambda *a, **k: None)
    monkeypatch.setattr(q, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(q, "_telemetry", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# r11 #4 — submission failure at member N
# ---------------------------------------------------------------------------

def test_submission_failure_marks_only_the_confirmed_failer(monkeypatch, _quiet):
    df0 = pd.DataFrame({"a": [1]})
    boom = RuntimeError("submit refused")
    monkeypatch.setattr(q, "get_session", lambda: _FakeSession(
        [_FakeJob(df=df0), boom]))  # sqls[1] raises at submission; 2,3 never reached
    with pytest.raises(q._BatchPartial) as ei:
        q._execute_batch(("s0", "s1", "s2", "s3"), "recent", "T")
    bp = ei.value
    assert set(bp.frames) == {0}              # survivor harvested
    assert set(bp.errors) == {1}              # ONLY the confirmed failer
    assert bp.pending == {2, 3}               # unsubmitted != failed


def test_gather_failure_has_no_pending(monkeypatch, _quiet):
    df0 = pd.DataFrame({"a": [1]})
    monkeypatch.setattr(q, "get_session", lambda: _FakeSession(
        [_FakeJob(df=df0), _FakeJob(exc=RuntimeError("died server-side"))]))
    with pytest.raises(q._BatchPartial) as ei:
        q._execute_batch(("s0", "s1"), "recent", "T")
    assert set(ei.value.errors) == {1} and ei.value.pending == set()


# ---------------------------------------------------------------------------
# r11 #4/#5 — quarantine membership + rehab (run_batch level)
# ---------------------------------------------------------------------------

def _fake_run_factory(ok=True):
    def _fake_run(sql, *, page, key, tier, source="", max_rows=None):
        return QueryResult(df=pd.DataFrame({"X": [1]}), ok=ok,
                           error="" if ok else "nope", source=source, tier=tier)
    return _fake_run


def _reset_quarantine(keys=()):
    import streamlit as st
    st.session_state["_ow_refresh_salt"] = "r11-test"
    st.session_state["_ow_batch_quarantine"] = {"salt": "r11-test", "keys": set(keys)}
    return st.session_state


def test_quarantine_only_confirmed_failers_not_pending(monkeypatch, _quiet):
    state = _reset_quarantine()
    monkeypatch.setattr(q, "run", _fake_run_factory(ok=True))

    def _raise_partial(sqls, scope, page):
        raise q._BatchPartial({0: pd.DataFrame({"A": [1]})},
                              {1: RuntimeError("confirmed")}, {2})
    monkeypatch.setitem(q._BATCH_FETCHERS, "recent", _raise_partial)
    out = q.run_batch([{"key": "k0", "sql": "s0"}, {"key": "k1", "sql": "s1"},
                       {"key": "k2", "sql": "s2"}], page="T", tier="recent")
    assert set(out) == {"k0", "k1", "k2"} and out["k0"].ok
    assert state["_ow_batch_quarantine"]["keys"] == {"k1"}  # k2 (pending) untainted


def test_clean_solo_run_rehabilitates_a_quarantined_key(monkeypatch, _quiet):
    state = _reset_quarantine(keys=["k9"])
    monkeypatch.setattr(q, "run", _fake_run_factory(ok=True))
    out = q.run_batch([{"key": "k9", "sql": "s9"}], page="T", tier="recent")
    assert out["k9"].ok
    assert "k9" not in state["_ow_batch_quarantine"]["keys"]  # r11 #5 rehab


def test_failed_solo_run_stays_quarantined(monkeypatch, _quiet):
    state = _reset_quarantine(keys=["k9"])
    monkeypatch.setattr(q, "run", _fake_run_factory(ok=False))
    out = q.run_batch([{"key": "k9", "sql": "s9"}], page="T", tier="recent")
    assert not out["k9"].ok
    assert "k9" in state["_ow_batch_quarantine"]["keys"]


# ---------------------------------------------------------------------------
# r11 #2 — mart_accept coverage gate
# ---------------------------------------------------------------------------

def _mk_run(results):
    """results: {marker: QueryResult} routed by substring of the sql."""
    def _fake_run(sql, *, page, key, tier, source="", max_rows=None):
        for marker, res in results.items():
            if marker in sql:
                return res
        raise AssertionError(f"unrouted sql {sql!r}")
    return _fake_run


def test_mart_accept_falls_through_to_live(monkeypatch):
    from app.ui.components import run_mart_first
    thin = QueryResult(df=pd.DataFrame({"MONTH": ["2026-06", "2026-07"]}), ok=True)
    full = QueryResult(df=pd.DataFrame({"MONTH": [f"2025-{m:02d}" for m in range(1, 13)]}), ok=True)
    monkeypatch.setattr(q, "run", _mk_run({"MARTQ": thin, "LIVEQ": full}))
    res = run_mart_first("MARTQ", "LIVEQ", page="T", key="k", mart_source="m",
                         live_source="l", mart_accept=lambda df: df["MONTH"].nunique() >= 12)
    assert len(res.df) == 12                   # live served


def test_mart_accept_keeps_mart_when_live_cannot_answer(monkeypatch):
    from app.ui.components import run_mart_first
    thin = QueryResult(df=pd.DataFrame({"MONTH": ["2026-07"]}), ok=True)
    dead = QueryResult(df=pd.DataFrame(), ok=False, error="no view")
    monkeypatch.setattr(q, "run", _mk_run({"MARTQ": thin, "LIVEQ": dead}))
    res = run_mart_first("MARTQ", "LIVEQ", page="T", key="k", mart_source="m",
                         live_source="l", mart_accept=lambda df: df["MONTH"].nunique() >= 12)
    assert res.ok and len(res.df) == 1         # partial mart beats empty panel


def test_mart_accept_predicate_errors_never_break_the_page(monkeypatch):
    from app.ui.components import run_mart_first
    thin = QueryResult(df=pd.DataFrame({"MONTH": ["2026-07"]}), ok=True)
    monkeypatch.setattr(q, "run", _mk_run({"MARTQ": thin}))
    res = run_mart_first("MARTQ", "LIVEQ", page="T", key="k", mart_source="m",
                         live_source="l", mart_accept=lambda df: df["NOPE"].nunique() >= 12)
    assert res.ok and len(res.df) == 1         # broken probe == accept mart


def test_overview_gates_the_boss_chart_on_month_coverage():
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert 'mart_accept=lambda df: df["MONTH"].nunique() >= 12' in ov
    assert "Dollars at today's" in ov          # r11 #11 rate label


# ---------------------------------------------------------------------------
# r11 #3 — prefs attempts only post-identity
# ---------------------------------------------------------------------------

def test_default_landing_burns_no_attempt_while_disconnected(monkeypatch):
    import streamlit as st

    import app.main as m
    for k in ("_ow_default_applied", "_ow_default_attempts", "_ow_current_user"):
        st.session_state.pop(k, None)
    calls = []
    monkeypatch.setattr(m, "run", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
        AssertionError("run() must not fire pre-identity")))
    m._apply_default_landing()
    assert not calls
    assert int(st.session_state.get("_ow_default_attempts", 0)) == 0
    assert not st.session_state.get("_ow_default_applied")      # will retry


def test_default_landing_counts_attempts_once_identity_exists(monkeypatch):
    import streamlit as st

    import app.main as m
    for k in ("_ow_default_applied", "_ow_default_attempts"):
        st.session_state.pop(k, None)
    st.session_state["_ow_current_user"] = "JFREEZE"
    monkeypatch.setattr(m, "run", _fake_run_factory(ok=False))
    m._apply_default_landing()
    assert int(st.session_state.get("_ow_default_attempts", 0)) == 1
    st.session_state.pop("_ow_current_user", None)


# ---------------------------------------------------------------------------
# r11 #7 — declarative canary gaps
# ---------------------------------------------------------------------------

def test_expected_gaps_reference_real_canaries_and_stay_feature_gated():
    from app.data.canary import CANARIES, EXPECTED_GAPS
    names = {n for n, _ in CANARIES}
    assert names >= EXPECTED_GAPS              # no phantom declarations
    assert all(n.startswith("cortex.") for n in EXPECTED_GAPS)
    core = {n for n in names if n.startswith(("mart.", "chargeback.", "recheck."))}
    assert not (core & EXPECTED_GAPS)          # core objects absent => FAIL


def test_admin_consumes_the_declaration():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "name in EXPECTED_GAPS" in adm
    assert "from app.data.canary import CANARIES, EXPECTED_GAPS" in adm


# ---------------------------------------------------------------------------
# r11 #1 — environment filter honesty (MGM lane reconcile rides with Compare)
# ---------------------------------------------------------------------------

def test_environment_claims_no_scope_it_does_not_apply():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    chip_block = comp.split("def _scope_chip_html", 1)[1].split("\ndef ", 1)[0]
    assert 'f["environment"]' not in chip_block          # chip no longer claims it
    mn = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "Narrows the Database picker only" in mn      # picker says what it does
    assert "· {_f['environment']} ·" not in mn           # scope stat stopped claiming
