"""Next-Fifty #7 Slice B (v4.590.0): every app statement carries its OWN QUERY_TAG on owner's-rights SiS.

ALTER SESSION is rejected there, so the session-level tag never applied in production. The owner's probe
(2026-09-24, an owner's-rights proc as the SiS proxy) proved Snowpark ``statement_params`` records the
QUERY_TAG and honors STATEMENT_TIMEOUT_IN_SECONDS, so the tag now rides each statement at every execute
seam. Off-SiS nothing changes (the ALTER SESSION path still tags, and the test fakes are never handed a
new kwarg). The per-statement timeout is enabled only for the tiers in STATEMENT_PARAMS_TIMEOUT_TIERS
(Cortex): the read tiers' ceilings were never enforced in production and stay off until measured."""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

import app.core.query as q
from app.core import ai, errors, session
from app.core.session import STATEMENT_PARAMS_TIMEOUT_TIERS, statement_params, submit_collect, submit_pandas
from app.data import mart_sql


class _Sis:
    """A session object that looks like the SiS one _connect() stamps."""
    _ow_is_sis = True

    def __init__(self, stmt=None):
        self.stmt = stmt
        self.sqls: list[str] = []

    def sql(self, text):
        self.sqls.append(text)
        return self.stmt


class _Job:
    def __init__(self, df):
        self._df = df
        self.query_id = "01abc"

    def result(self):
        return self._df


class _Stmt:
    """Records every submit's kwargs; optionally rejects statement_params like an old Snowpark."""

    def __init__(self, *, reject_params=False, rows=None):
        self.calls: list[tuple[str, dict]] = []
        self.reject = reject_params
        self.rows = rows if rows is not None else [{"ANSWER": "ok"}]

    def _check(self, kw):
        if self.reject and "statement_params" in kw:
            raise TypeError("to_pandas() got an unexpected keyword argument 'statement_params'")

    def to_pandas(self, **kw):
        self._check(kw)
        self.calls.append(("to_pandas", kw))
        return _Job(pd.DataFrame({"a": [1]})) if kw.get("block") is False else pd.DataFrame({"a": [1]})

    def collect(self, **kw):
        self._check(kw)
        self.calls.append(("collect", kw))
        return self.rows

    def collect_nowait(self, **kw):
        self._check(kw)
        self.calls.append(("collect_nowait", kw))
        return object()


@pytest.fixture
def _quiet(monkeypatch):
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q, "apply_statement_timeout", lambda *a, **k: None)
    monkeypatch.setattr(q, "record_error", lambda *a, **k: None)


def test_off_sis_is_untouched():
    class _Plain:
        pass
    assert statement_params(_Plain(), page="Ops", tier="live") is None
    assert statement_params(None, page="Ops", tier="live") is None


def test_sis_tag_and_timeout_policy():
    s = _Sis()
    assert statement_params(s, page="Operations", tier="live", timeout_s=30) == {
        "QUERY_TAG": "OVERWATCH|page=Operations|tier=live"}          # read tiers: tag only, no timeout
    assert set(STATEMENT_PARAMS_TIMEOUT_TIERS) == {"cortex"}
    p = statement_params(s, page="AI", tier="cortex", timeout_s=90)
    assert p == {"QUERY_TAG": "OVERWATCH|page=AI|tier=cortex", "STATEMENT_TIMEOUT_IN_SECONDS": "90"}
    assert statement_params(s, page="AI", tier="cortex", timeout_s=5)["STATEMENT_TIMEOUT_IN_SECONDS"] == "10"
    assert statement_params(s, page="AI", tier="cortex", timeout_s=5000)["STATEMENT_TIMEOUT_IN_SECONDS"] == "900"
    # the page is sanitized exactly like the session tag (no '|' injection into the tag grammar)
    assert statement_params(s, page="a|b'c", tier="live")["QUERY_TAG"] == "OVERWATCH|page=abc|tier=live"


def test_execute_passes_the_tag(monkeypatch, _quiet):
    stmt = _Stmt()
    monkeypatch.setattr(q, "get_session", lambda: _Sis(stmt))
    out = q._execute("SELECT 1", "live", "Operations")
    assert list(out.columns) == ["A"]
    (name, kw), = stmt.calls
    assert name == "to_pandas" and kw["block"] is False
    assert kw["statement_params"] == {"QUERY_TAG": "OVERWATCH|page=Operations|tier=live"}


def test_batch_members_are_tagged(monkeypatch, _quiet):
    stmt = _Stmt()
    monkeypatch.setattr(q, "get_session", lambda: _Sis(stmt))
    q._execute_batch(("s0", "s1"), "recent", "Brief")
    assert [kw["statement_params"]["QUERY_TAG"] for _, kw in stmt.calls] == ["OVERWATCH|page=Brief|tier=recent"] * 2
    assert all(kw["block"] is False for _, kw in stmt.calls)


def test_old_snowpark_rejection_resubmits_untagged_once(monkeypatch, _quiet):
    stmt = _Stmt(reject_params=True)
    sis = _Sis(stmt)
    monkeypatch.setattr(q, "get_session", lambda: sis)
    q._execute("SELECT 1", "live", "Ops")
    assert stmt.calls == [("to_pandas", {"block": False})]           # the untagged resubmit ran once
    assert getattr(sis, session._PARAMS_ATTR) is False                # remembered on the session...
    assert statement_params(sis, page="Ops", tier="live") is None     # ...so later statements skip params


def test_unrelated_type_error_propagates():
    class _Bad:
        def collect(self, **kw):
            raise TypeError("unsupported operand type(s)")
    sis = _Sis()
    with pytest.raises(TypeError, match="unsupported operand"):
        submit_collect(sis, _Bad(), {"QUERY_TAG": "OVERWATCH"})
    assert getattr(sis, session._PARAMS_ATTR, None) is None           # NOT mistaken for a rejection


def test_submit_helpers_pass_nothing_new_without_params():
    stmt = _Stmt()
    submit_pandas(None, stmt, None)
    submit_collect(None, stmt, None, nowait=True)
    assert stmt.calls == [("to_pandas", {}), ("collect_nowait", {})]   # exactly the pre-Slice-B calls


def test_write_seam_tags_the_statement(monkeypatch, _quiet):
    stmt = _Stmt()
    monkeypatch.setattr(q, "get_session", lambda: _Sis(stmt))
    monkeypatch.setattr(q, "_bump_refresh", lambda *_a, **_k: None)
    ok, _msg = q.execute_statement("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (A) VALUES (1)", page="Alerts")
    assert ok
    (name, kw), = stmt.calls
    assert name == "collect" and kw["statement_params"] == {"QUERY_TAG": "OVERWATCH|page=Alerts|tier=write"}


def test_cortex_carries_its_ceiling(monkeypatch):
    stmt = _Stmt()
    monkeypatch.setattr(ai, "get_session", lambda: _Sis(stmt))
    monkeypatch.setattr(ai, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(ai, "apply_statement_timeout", lambda *a, **k: None)
    ok, answer = ai.cortex_complete("why is the warehouse idle?", page="Cost")
    assert ok and answer == "ok"
    (_, kw), = stmt.calls
    assert kw["statement_params"] == {"QUERY_TAG": "OVERWATCH|page=Cost|tier=cortex",
                                      "STATEMENT_TIMEOUT_IN_SECONDS": str(ai.CORTEX_TIMEOUT_SECONDS)}


def test_error_sink_tags_its_insert(monkeypatch):
    stmt = _Stmt()
    monkeypatch.setattr(session, "get_cached_session", lambda: _Sis(stmt))
    errors.record_error("Security", RuntimeError("boom"), context="t")
    (name, kw), = stmt.calls
    assert name == "collect_nowait"
    assert kw["statement_params"] == {"QUERY_TAG": "OVERWATCH|page=Security|tier=write"}


def test_every_seam_routes_through_the_transport():
    for fn in (q._execute, q._execute_batch, q.execute_statement, q.execute_statement_async,
               q.execute_cancel_query, q.execute_action, ai.cortex_complete, errors.record_error,
               session.current_role):
        src = inspect.getsource(fn)
        assert "statement_params(" in src, fn.__name__
        # no bare submit left behind: every Snowpark submit goes through submit_pandas/submit_collect
        for bare in (".to_pandas(block", ".to_pandas()", ".collect()", ".collect_nowait()"):
            assert bare not in src, (fn.__name__, bare)


def test_self_cost_split_names_the_other_side_honestly():
    assert mart_sql.APP_OTHER_WORKLOAD == "TASKS / ALERTS / OTHER"
    for sql in (mart_sql.app_self_cost(14), mart_sql.app_warehouse_queue_by_hour(14)):
        assert "'TASKS / ALERTS / OTHER'" in sql and "UNTAGGED APP" not in sql
        # review fix: the SiS runtime statement and the connector's untagged async result fetch are the
        # app's too - no client-side tag reaches either, so they get their own buckets
        assert "STARTSWITH(UPPER(COALESCE(QUERY_TEXT, '')), 'EXECUTE STREAMLIT')" in sql
        assert "'APP RUNTIME (SiS)'" in sql
        assert "STARTSWITH(LOWER(COALESCE(QUERY_TEXT, '')), 'select * from table(result_scan(''')" in sql
        assert "'APP RESULT FETCH (untagged)'" in sql
        assert sql.index("'INTERACTIVE APP'") < sql.index("'APP RUNTIME (SiS)'") < sql.index(
            "'APP RESULT FETCH (untagged)'") < sql.index("'TASKS / ALERTS / OTHER'")
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(mart_sql.app_self_cost(14), read="snowflake")


def test_result_fetch_arm_matches_the_connector_text():
    # the exact statement snowflake.connector's get_results_from_sfqid issues after an async job
    fetch = "select * from table(result_scan('01b2c3d4-0000-1111-0000-000000000001'))"
    assert fetch.lower().startswith("select * from table(result_scan('")


def test_app_cortex_self_cost_builder():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.app_cortex_self_cost(999)
    assert "DATEADD('day', -30," in sql and "DATEADD('day', -31," in sql          # clamped to 30d
    assert "CORTEX_AI_FUNCTIONS_USAGE_HISTORY" in sql and "QUERY_HISTORY Q ON Q.QUERY_ID = ai.QUERY_ID" in sql
    assert "(Q.QUERY_TAG LIKE 'OVERWATCH%')" in sql and "QUERY_TEXT" not in sql     # tag-only join
    assert "SUM(SUM(ai.AI_CREDITS)) OVER ()" in sql and "SUM(COUNT(*)) OVER ()" in sql   # uncapped totals
    assert "FLATTEN" not in sql                                                     # no METRICS fan-out
    sqlglot.parse_one(sql, read="snowflake")


def test_admin_cortex_card_is_toggle_and_probe_gated():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/ui/pages/admin.py").read_text(encoding="utf-8")
    body = src.split("def _app_cortex_cost(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("st.toggle(") < body.index("run(mart_sql.app_cortex_self_cost(30)")
    assert "probe=True" in body and '"method": "AI rate"' in body and "AI_CREDIT_PRICE_USD" in body
    # review fix: sub-cent spend is shown precisely, never as a rounded $0.00 / 0.00
    assert "format_usd_precise(float(_cr) * ai_rate)" in body
    assert 'format="%.6f"' in body and 'format="$%.4f"' in body
    assert 'else "—"' in body                                     # zero/unknown never renders a false $0
    tab = src.split("def _self_cost_tab(", 1)[1].split("\ndef ", 1)[0]
    perf = src.split("def _performance_tab(", 1)[1].split("\ndef ", 1)[0]
    assert "Every app query is instead" not in perf and "{CORTEX_TIMEOUT_SECONDS}s per-statement" in perf
    assert tab.index("_run_cost_panel()") < tab.index("_app_cortex_cost()")
