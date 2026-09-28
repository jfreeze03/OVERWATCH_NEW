"""Production-shaped render contracts (review #2).

The empty-stub smoke (test_pages_apptest.py) exercises only the honest-EMPTY branches;
the populated branches that index specific columns (df["CREDITS_BILLED"], iloc[0][...])
never run, so a renamed/missing column raising KeyError in a populated table/chart passes
CI. This harness stubs run() to return a small, correctly-SHAPED frame instead of an
empty one — columns parsed from each builder's own SELECT, dtypes chosen by the same
name convention the app formats on — so the populated branches actually render and a
column contract break surfaces as a test failure.

Teeth: the shaped frame carries EXACTLY the builder's SELECT columns, so a page that
indexes a column its builder does not return raises KeyError here. Coverage: run(),
run_batch, run_batch_mixed AND run_mart_first are all shaped across every read-doing UI
module, so batched panels (Control Room, Operations, Cost) render their populated
branches too. The v4.431 startup schema gate is bypassed here (it has its own tests) so
pages render their bodies rather than the below-floor blocked state.
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from packaging.version import parse as _parse_version  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.config import PAGES_BY_PROFILE  # noqa: E402
from app.core.result import QueryResult  # noqa: E402

_APPTEST_BUTTONGROUP_OK = _parse_version(st.__version__) >= _parse_version("1.55.0")

# Every DBA page, rendered with SHAPED (non-empty, correctly-typed) data so the
# populated column-indexing branches actually execute — not just the empty branches.
_SHAPED_PAGES = list(PAGES_BY_PROFILE["DBA"])

_STR_TOKENS = (
    "NAME", "USER", "ROLE", "DATABASE", "SCHEMA", "WAREHOUSE", "STATUS", "DESCRIPTION",
    "TITLE", "LABEL", "CATEGORY", "KIND", "TYPE", "COMPANY", "OWNER", "SERVICE", "REASON",
    "NOTE", "VERDICT", "SEVERITY", "METRIC", "TAG", "CLOUD", "REGION", "MODEL", "FUNCTION",
    "SOURCE", "TARGET", "HASH", "KEY", "QUERY_ID", "EVENT_ID", "INCIDENT_ID", "TEXT",
    "MESSAGE", "PROFILE", "DEPARTMENT", "STEWARD", "CONDITION", "HYPOTHESIS", "ROUTE",
)
_DATE_TOKENS = ("_AT", "_TS", "_TIME", "DAY", "DATE", "_DAY", "_HOUR", "HOUR_TS", "WEEK", "MONTH")


def _col_value(col: str, row: int):
    u = col.upper()
    # HOUR_OF_DAY (a 0-23 clock hour) and any *DAYS* count are INTEGERS, not dates —
    # but "DAY" is a substring of their names, so the date rule below would mis-type
    # them as Timestamps and break int(hour) / float(days) in the warehouse
    # quiet-hours + adaptive-candidacy panels. Plural "DAYS" is always a count in this
    # schema; singular "DAY"/"DATE"/"LATEST_DAY" stays a real date below.
    if "HOUR_OF_DAY" in u or "DAYS" in u:
        return float(row % 24) if "HOUR" in u else float(row + 1)
    if u in ("DAY", "DATE", "HOUR_TS") or any(u.endswith(t) or t in u for t in _DATE_TOKENS):
        return pd.Timestamp("2026-08-15") + datetime.timedelta(days=row)
    if any(t in u for t in _STR_TOKENS):
        # QUERY_ID-like columns get an id-shaped string so drill links build
        return f"{col}_{row}"
    return float(row + 1)          # numeric default: 1.0, 2.0 — non-degenerate for sort/delta


def _columns_for(sql: str) -> list[str]:
    try:
        import sqlglot
        expr = sqlglot.parse_one(sql, read="snowflake")
        return [c for c in expr.named_selects if c and c != "*"]
    except Exception:  # noqa: BLE001 - any parse failure (scripting/$$, dialect) -> empty frame
        return []


def _shaped_from_sql(sql, source="stub"):
    cols = _columns_for(str(sql or ""))
    if not cols:
        return QueryResult(df=pd.DataFrame(), ok=True, source=str(source))
    df = pd.DataFrame({c: [_col_value(c, r) for r in range(2)] for c in cols})
    return QueryResult(df=df, ok=True, source=str(source))


def _shaped_run(*args, **kwargs):
    return _shaped_from_sql(args[0] if args else kwargs.get("sql", ""), kwargs.get("source", "stub"))


def _shaped_batch(specs, **_kwargs):
    # run_batch / run_batch_mixed contract: {key: QueryResult} with every key present.
    return {s.get("key"): _shaped_from_sql(s.get("sql", ""), s.get("source", "stub"))
            for s in (specs or [])}


def _shaped_mart_first(mart_sql, live_sql="", **kwargs):
    res = _shaped_from_sql(mart_sql, kwargs.get("mart_source", "stub"))
    if res.df.empty:                                    # unparseable mart SQL -> try the live twin
        res = _shaped_from_sql(live_sql, kwargs.get("live_source", "stub"))
    return res


def _fake_execute(*_args, **_kwargs):
    return True, "stubbed"


# Every read entry point pages call, mapped to its shaped stub. Patched per module
# because pages import these names directly (a patch of the defining module would not
# rebind an already-imported name).
_READ_STUBS = {
    "run": _shaped_run,
    "run_batch": _shaped_batch,
    "run_batch_mixed": _shaped_batch,
    "run_mart_first": _shaped_mart_first,
    "execute_statement": _fake_execute,
}


@pytest.fixture(autouse=True)
def _stub_shaped(monkeypatch):
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, attention, components, security_center, workbench
    from app.ui import decision_studio as ds_render
    from app.ui.pages import (
        admin,
        alerts,
        ask,
        brief,
        control_room,
        cost,
        decision_studio,
        operations,
        overview,
        security,
    )
    from app.ui.pages.cost_parts import ai_chargeback, compare, contract, optimize, spend, unit_costs
    from app.ui.pages.ops_parts import optimize_queue

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_SYSADMINS")
    # The v4.431 startup schema gate reads SCHEMA_VERSION; a shaped VERSION column would
    # look below the floor and block every page with the migration banner. This harness
    # tests page BODIES, so bypass the gate (it has its own dedicated tests).
    monkeypatch.setattr(main_mod, "_schema_floor_breach", lambda: None)

    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"

    for module in (main_mod, components, ai_panel, ds_render, security_center, workbench, attention,
                   overview, control_room, cost, operations, alerts, security, admin, brief,
                   ask, decision_studio, ai_chargeback, compare, contract, optimize, spend,
                   unit_costs, optimize_queue):
        for fname, fstub in _READ_STUBS.items():
            if hasattr(module, fname):
                monkeypatch.setattr(module, fname, fstub)
        if hasattr(module, "current_role"):
            monkeypatch.setattr(module, "current_role", lambda: "SNOW_SYSADMINS")
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))
    # v4.599: the buffered telemetry flush reaches get_session() -> st.connection('snowflake'); with a
    # default connection configured, a shaped render would INSERT real telemetry. Swallow async writes.
    import app.core.query as query_mod
    monkeypatch.setattr(query_mod, "execute_statement_async", lambda *a, **k: True)


def _entry():
    import app.main
    app.main.main()


def _nav_to(at, page: str) -> None:
    for r in at.radio:
        if str(getattr(r, "key", "") or "").startswith("_ow_nav_") and page in list(r.options):
            r.set_value(page)
            return
    raise AssertionError(f"page {page!r} not offered in any nav group")


def test_shaper_returns_exactly_the_declared_columns_typed():
    """Prove the harness has teeth: the shaped frame carries EXACTLY the builder's
    SELECT columns (so indexing an undeclared column raises KeyError, the contract this
    enforces) and types them by the app's own name convention."""
    res = _shaped_run("SELECT a AS FOO, SUM(b) AS BAR FROM t GROUP BY a")
    assert list(res.df.columns) == ["FOO", "BAR"] and not res.df.empty
    with pytest.raises(KeyError):
        _ = res.df["NOT_SELECTED"]
    res2 = _shaped_run("SELECT x AS SPEND_USD, y AS USER_NAME, z AS DAY FROM t")
    assert pd.api.types.is_numeric_dtype(res2.df["SPEND_USD"])
    assert pd.api.types.is_object_dtype(res2.df["USER_NAME"])
    assert pd.api.types.is_datetime64_any_dtype(res2.df["DAY"])
    # a builder whose SQL cannot be parsed degrades to an empty frame (never raises)
    assert _shaped_run("EXECUTE IMMEDIATE $$ BEGIN NULL; END $$").df.empty


def test_batched_stubs_shape_every_member():
    """run_batch / run_batch_mixed / run_mart_first are shaped too, so the populated
    branches behind BATCHED reads (Control Room, Operations, Cost) actually execute."""
    batch = _shaped_batch([{"key": "a", "sql": "SELECT x AS FOO FROM t"},
                           {"key": "b", "sql": "SELECT y AS BAR FROM t"}])
    assert set(batch) == {"a", "b"}
    assert list(batch["a"].df.columns) == ["FOO"] and not batch["a"].df.empty
    mf = _shaped_mart_first("SELECT c AS BAZ FROM m", "SELECT c AS BAZ FROM live")
    assert list(mf.df.columns) == ["BAZ"] and not mf.df.empty
    # an unparseable mart SQL falls back to the live twin's shape (never returns empty vacuously)
    mf2 = _shaped_mart_first("EXECUTE IMMEDIATE $$ x $$;", "SELECT q AS QUX FROM live")
    assert list(mf2.df.columns) == ["QUX"]


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page", _SHAPED_PAGES)
def test_pages_render_with_shaped_data(page):
    at = AppTest.from_function(_entry, default_timeout=25)
    at.run()
    assert not at.exception
    _nav_to(at, page)
    at.run()
    assert not at.exception, f"{page} (shaped): {at.exception}"
    # the page rendered its BODY, not the startup schema-gate blocked state (proving the
    # gate bypass held and the populated branches actually ran)
    assert not any("migrated through" in str(getattr(e, "value", "")) for e in at.error), \
        f"{page}: schema gate blocked the render"
    assert at.title or at.markdown, page


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_operations_warehouses_sizing_lens_renders_shaped():
    """deferred-item (Warehouses sub-nav): the tab's SECOND nested lens ('Sizing &
    efficiency') is not the default, so test_pages_render_with_shaped_data only ever
    paints the 'Activity & anomalies' lens. Drive the sizing lens AND its two heavy-scan
    toggles explicitly, so the extracted sizing / cost-per-query / quiet-hours / monitor
    / adaptive-candidacy panels actually execute under shaped data (the coverage a
    default-lens-only render can't give)."""
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Operations")
    at.session_state["ops_section"] = "Warehouses"        # top-level section
    at.session_state["ops_wh_view"] = "Sizing & efficiency"  # the non-default nested lens
    at.session_state["ops_wh_sizing_load"] = True         # utilization profile + cost-per-query
    at.session_state["ops_wh_quiet_load"] = True          # quiet-hours heatmap
    at.run()
    assert not at.exception, f"warehouses sizing lens (shaped): {at.exception}"
    # the page finished (no safe_page 'could not finish rendering' error caption)
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error), \
        "the sizing lens raised mid-render"
    blob = " ".join(m.value for m in at.markdown)
    # the sizing lens painted its own panels — the split is reachable AND the extracted
    # sizing/adaptive panels execute (the coverage the default 'Activity' lens can't give)
    assert "Utilization &amp; right-sizing" in blob or "Utilization & right-sizing" in blob, \
        "sizing lens did not paint its right-sizing header"
    assert "Adaptive-compute candidacy" in blob, "adaptive-candidacy panel did not paint"


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_operations_optimize_renders_shaped():
    """v4.597 (Option C): Operations > Optimize is not the default section, so the page sweep never
    paints it. Drive it with the live-profile toggle ON, an operator role and a selected family, so
    the fix-queue body, the live-diagnosis merge, the Track-all bar and the detail pane's Track
    block all execute under shaped data (a column the builders do not return raises here)."""
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Operations")
    at.session_state["ops_section"] = "Optimize"
    at.session_state["ops_opt_live"] = True
    at.session_state["_ow_current_role"] = "SNOW_SYSADMINS"     # DBA profile -> operator (off-SiS)
    at.session_state["_ow_md_sel_ops_optimize"] = "1.0"         # shaped FINGERPRINT of row 0
    at.run()
    assert not at.exception, f"operations optimize (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error),         "the Optimize section raised mid-render"
    blob = " ".join(m.value for m in at.markdown)
    assert "Fix queue" in blob, "the fix-queue header did not paint"
    assert "First fix:" in blob, "the detail pane did not render the selected family"
    labels = [str(b.label) for b in at.button]
    assert any(lbl.startswith("Track all ACT NOW (") for lbl in labels), labels
    assert "Track" in labels, "the operator Track button did not render in the detail pane"


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_operations_optimize_failed_tracked_read_is_unknown(monkeypatch):
    """Review r2: when the Action Center status read FAILS, the queue says so, shows every family as
    Unknown (not Untracked) and holds Track all at 0 -- it never states tracking facts it could not read."""
    from app.ui.pages.ops_parts import optimize_queue

    def _batch_tracked_fails(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        out["ops_opt_tracked"] = QueryResult(df=pd.DataFrame(), ok=False, error="stub timeout",
                                             source="stub")
        return out

    monkeypatch.setattr(optimize_queue, "run_batch_mixed", _batch_tracked_fails)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Operations")
    at.session_state["ops_section"] = "Optimize"
    at.session_state["_ow_current_role"] = "SNOW_SYSADMINS"
    at.session_state["_ow_md_sel_ops_optimize"] = "1.0"
    at.run()
    assert not at.exception, f"operations optimize (tracked read failed): {at.exception}"
    blob = " ".join(str(m.value) for m in list(at.markdown) + list(at.caption) + list(at.warning)
                    + list(at.info) + list(at.error))
    assert "Action Center status could not be read" in blob
    assert "Action Center: status unknown (the read failed)." in blob
    assert "Action Center: Untracked." not in blob
    labels = [str(b.label) for b in at.button]
    assert "Track all ACT NOW (0)" in labels, labels


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_etl_configured_morning_surfaces_render(monkeypatch):
    """Next-Fifty #1: with the default (empty) ETL_CONTROL_STATUS_FQN the whole-night glance, the
    Brief tile's populated branches and the shared attention verdict stay dormant. Configure it and
    render Brief, Control Room and Operations ▸ Pipeline SLA (default 'Tonight' chapter) under shaped
    data so those branches actually execute."""
    from app.config import DEFAULT_SETTINGS
    from app.ui import components
    from app.ui.pages import brief, control_room, operations

    etl_settings = dict(DEFAULT_SETTINGS)
    etl_settings.update({"_source": "stub", "ETL_CONTROL_STATUS_FQN": "DB.SCH.CONTROL_STATUS"})
    for mod in (brief, control_room, operations, components):
        monkeypatch.setattr(mod, "load_settings", lambda _page: dict(etl_settings))
    for page, state in (("Brief", {}), ("Control Room", {}),
                        ("Operations", {"ops_section": "Pipeline SLA"})):
        at = AppTest.from_function(_entry, default_timeout=30)
        at.run()
        assert not at.exception
        _nav_to(at, page)
        for k, v in state.items():
            at.session_state[k] = v
        at.run()
        assert not at.exception, f"{page} (ETL configured, shaped): {at.exception}"
        assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error), page
    blob = " ".join(m.value for m in at.markdown)
    assert "Tonight at a glance" in blob, "the whole-night glance did not paint on Operations"
    # v4.597 (Option C): the two read-only built-in objectives paint at the top of Tonight.
    assert "Built-in objectives" in blob, "the built-in objectives panel did not paint on Tonight"


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_proof_sections_render_shaped():
    """v4.597 (Option C): drive BOTH Proof sections under shaped data — the merged Proof tab (hero,
    run-rate, per-item evidence + the SQL attribution split) and the Pipeline tab with right-sizing
    on (addressable rollup + queued work + the projection fragment), then press "Reset to measured"
    so the fragment's on_click callback path executes too. A column a builder does not return
    raises here."""
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Proof")
    at.run()
    assert not at.exception, f"proof (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    blob = " ".join(m.value for m in at.markdown)
    assert "What each saving rests on" in blob and "active run-rate" in blob, blob[:400]
    at.session_state["decision_section"] = "Pipeline"
    at.session_state["proof_pipe_sizing"] = True
    at.run()
    assert not at.exception, f"proof pipeline (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    blob = " ".join(m.value for m in at.markdown)
    assert "the priced pipeline" in blob
    keys = {str(s.key) for s in at.slider}
    assert {"proof_adoption", "proof_realization", "proof_conf_floor"} <= keys, keys
    reset = [b for b in at.button if str(b.label) == "Reset to measured"]
    assert reset, [str(b.label) for b in at.button]
    at.slider(key="proof_adoption").set_value(5)
    at.run()
    assert not at.exception
    reset = [b for b in at.button if str(b.label) == "Reset to measured"]
    reset[0].click()
    at.run()
    assert not at.exception, f"proof reset-to-measured (shaped): {at.exception}"
    assert at.slider(key="proof_adoption").value != 5      # the callback restored the default


def _admin_section(section: str, **state) -> AppTest:
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Admin")
    at.session_state["adm_section"] = section
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"admin {section} (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error), \
        f"admin {section} raised mid-render"
    return at


def _texts(at) -> str:
    return " ".join(str(e.value) for e in list(at.markdown) + list(at.caption) + list(at.warning)
                    + list(at.info) + list(at.error))


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_admin_migrations_task_health_renders_shaped(monkeypatch):
    """v4.599 (#26): Migrations & freshness with Task health switched ON. Under the shaped stub the SHOW
    TASKS read parses as a Command (no columns -> empty frame), which exercises the NOT_VISIBLE branch;
    then a realistic SHOW frame drives the graded table (NaN counts / NaT times through styled_table,
    a suspended task, a failing one with a '$' in its error) so the populated branch renders too."""
    at = _admin_section("Migrations & freshness", adm_task_health=True)
    blob = _texts(at)
    assert "Task health" in blob
    assert "SHOW TASKS lists no OVERWATCH tasks" in blob, "empty SHOW did not read as not-visible"
    assert "Runtime: Python " in blob, "the runtime caption did not paint in the Admin header"

    from app.data import ops_sql
    from app.ui.pages import admin
    names = sorted(ops_sql.OVERWATCH_TASKS)
    show = pd.DataFrame({"name": names, "state": ["started"] * len(names),
                         "schedule": [None] * len(names), "predecessors": ["[]"] * len(names)})
    show.loc[show["name"] == "TASK_QH_EXTRACT", "state"] = "suspended"
    runs = pd.DataFrame({"TASK_NAME": ["TASK_LOAD_DAILY", None], "SUCCEEDED_N": [0, 0], "FAILED_N": [2, 0],
                         "SKIPPED_N": [0, 0], "LAST_SUCCESS_AT": [pd.NaT, pd.NaT],
                         "LAST_FAILURE_AT": [pd.Timestamp("2026-09-28 06:50"), pd.NaT],
                         "LAST_ERROR_MESSAGE": ["Numeric value '$1' is not recognized", None],
                         "HISTORY_ROWS": [40, 40]})
    shaped_run = admin.run

    def _task_run(*args, **kwargs):
        if kwargs.get("key") == "adm_task_states":
            return QueryResult(df=show.copy(), ok=True, source="stub")
        if kwargs.get("key") == "adm_task_runs":
            return QueryResult(df=runs.copy(), ok=True, source="stub")
        return shaped_run(*args, **kwargs)

    monkeypatch.setattr(admin, "run", _task_run)
    at = _admin_section("Migrations & freshness", adm_task_health=True)
    blob = _texts(at)
    assert "OVERWATCH tasks started" in blob and "1 failing (last 24h)" in blob, blob[-600:]
    assert any("TASK_LOAD_DAILY" in str(df.value.to_string()) for df in at.dataframe), \
        "the graded task table did not render"


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
# v4.599 (#50/#47): the 'Section visits' and 'Ask demand' panels (admin._usage_detail_panels).
def test_admin_performance_usage_panels_render_shaped():
    """v4.599: Admin > Performance under shaped data paints the new usage panels (Section visits,
    Ask demand) beside Page adoption, with the #33 timeout wording, and does not raise mid-render."""
    at = _admin_section("Performance")
    blob = _texts(at)
    assert "Section visits" in blob, "the Section visits panel header did not paint"
    assert "Ask demand" in blob, "the Ask demand panel header did not paint"


def _nav_context(at) -> dict:
    """The page-local nav context left after a render ({} when none) -- AppTest state has no .get()."""
    try:
        return dict(at.session_state["_ow_nav_context"] or {})
    except KeyError:
        return {}


_EVIDENCE_MARK = "ON k.SESSION_ID = c.SESSION_ID"     # only etl_control_sql.run_task_evidence_scan emits it


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_task_evidence_drill_renders_shaped_and_is_off_by_default(monkeypatch):
    """Next-Fifty #14 Ph1: Operations ▸ Pipeline SLA ▸ Tonight with the ETL tables configured. Off by
    default, neither 'Explain a task' drill reads (or renders its picker); switched on at both sites
    (Workflow runtimes + Run inventory), both read, the page finishes, and the shaped failed CALL leads
    with its error."""
    from app.config import DEFAULT_SETTINGS
    from app.ui import components
    from app.ui.pages import operations

    etl_settings = dict(DEFAULT_SETTINGS)
    etl_settings.update({"_source": "stub", "ETL_CONTROL_STATUS_FQN": "DB.SCH.CONTROL_STATUS",
                         "ETL_CONTROL_RUN_ID_FQN": "DB.SCH.CONTROL_RUN_ID"})
    for mod in (operations, components):
        monkeypatch.setattr(mod, "load_settings", lambda _page: dict(etl_settings))
    seen: list[str] = []

    def _recording_run(*args, **kwargs):
        seen.append(str(args[0] if args else kwargs.get("sql", "")))
        return _shaped_run(*args, **kwargs)

    monkeypatch.setattr(operations, "run", _recording_run)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Operations")
    at.session_state["ops_section"] = "Pipeline SLA"
    at.run()
    assert not at.exception, f"pipeline SLA tonight (shaped): {at.exception}"
    assert not any(_EVIDENCE_MARK in s for s in seen), "an evidence read ran with the drill switched off"
    assert {"etl_ev_rt_toggle", "etl_ev_inv_toggle"} <= {str(t.key) for t in at.toggle}
    assert not {"etl_ev_rt_pick", "etl_ev_inv_pick"} & {str(s.key) for s in at.selectbox}
    seen.clear()
    at.session_state["etl_ev_rt_toggle"] = True
    at.session_state["etl_ev_inv_toggle"] = True
    at.run()
    assert not at.exception, f"task evidence drill (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    assert len([s for s in seen if _EVIDENCE_MARK in s]) >= 2, "both drills should have read"
    assert {"etl_ev_rt_pick", "etl_ev_inv_pick"} <= {str(s.key) for s in at.selectbox}
    assert any("Failed:" in str(e.value) for e in at.error), [str(e.value) for e in at.error]


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_queries_opportunity_row_opens_in_optimize(monkeypatch):
    """#28: select a row on Operations ▸ Queries' opportunity board, click "Open in Optimize →": the SAME
    family lands selected on Operations ▸ Optimize with the live profile on (its diagnosis matches the QOP
    breakdown just read), and the one-shot context is fully consumed."""
    from app.ui.pages import operations

    real_select = operations.selectable_table
    captured: dict[str, str] = {}

    def _select_first(df, key, **kwargs):
        if key == "ops_qopp_sel":
            captured["fp"] = str(df.iloc[0]["FINGERPRINT"])     # never hard-code: the harness types it
            return 0
        return real_select(df, key, **kwargs)

    monkeypatch.setattr(operations, "selectable_table", _select_first)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Operations")
    at.session_state["ops_qopp_toggle"] = True
    at.run()
    assert not at.exception, f"queries opportunity board (shaped): {at.exception}"
    buttons = [b for b in at.button if str(b.label) == "Open in Optimize →"]
    assert len(buttons) == 1, [str(b.label) for b in at.button]
    buttons[0].click()
    at.run()
    assert not at.exception, f"open in optimize (shaped): {at.exception}"
    assert at.session_state["ops_section"] == "Optimize"
    assert at.session_state["_ow_md_sel_ops_optimize"] == captured["fp"]
    assert at.session_state["ops_opt_live"] is True
    ctx = _nav_context(at)
    assert "fingerprint" not in ctx and "live_profile" not in ctx, ctx
    blob = " ".join(str(m.value) for m in at.markdown)
    assert "First fix:" in blob, "the landed family's detail pane did not render"
    assert "from the live query profile" in blob, "the landing diagnosis is not the live one"


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_optimize_names_a_family_it_does_not_queue():
    """#28: a deep link to a family the fix queue does not list lands with an explanation in the empty
    detail pane (not a previously selected, unrelated family), is consumed on arrival, and the notice is
    delivered once (gone on the next rerun). Before #28 the link lingered silently."""
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Operations")
    at.run()
    at.session_state["_ow_md_sel_ops_optimize"] = "1.0"          # an earlier pick of a queued family
    at.session_state["_ow_nav_pending"] = {"page": "Operations", "section": "Optimize", "filters": {},
                                           "context": {"fingerprint": "NOT_IN_QUEUE"}, "origin": None}
    at.run()
    assert not at.exception, f"optimize unqueued link (shaped): {at.exception}"
    assert at.session_state["ops_section"] == "Optimize"
    blob = _texts(at)
    assert "Query family NOT_IN_QUEUE is not in this fix queue" in blob, blob[-600:]
    assert "First fix:" not in blob, "an unrelated family's detail pane is still showing"
    ctx = _nav_context(at)
    assert "fingerprint" not in ctx, ctx
    at.run()
    assert not at.exception
    assert "is not in this fix queue" not in _texts(at), "the notice lingered past its arrival"


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_optimize_keeps_a_deep_link_through_a_failed_queue_read(monkeypatch):
    """#28: when the fix-queue read fails, Optimize returns at its guard BEFORE consuming the link, so the
    deep-linked family (and its one-shot live_profile) still land on the next successful rerun."""
    from app.ui.pages.ops_parts import optimize_queue

    state = {"fail": True}

    def _batch(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        if state["fail"]:
            for k in [k for k in out if str(k).startswith("ops_opt_queue_")]:
                out[k] = QueryResult(df=pd.DataFrame(), ok=False, error="stub timeout", source="stub")
        return out

    monkeypatch.setattr(optimize_queue, "run_batch_mixed", _batch)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Operations")
    at.run()
    at.session_state["_ow_nav_pending"] = {"page": "Operations", "section": "Optimize", "filters": {},
                                           "context": {"fingerprint": "1.0", "live_profile": True},
                                           "origin": None}
    at.run()
    assert not at.exception, f"optimize failed queue read (shaped): {at.exception}"
    assert _nav_context(at) == {"fingerprint": "1.0", "live_profile": True}, "the link was consumed early"
    state["fail"] = False
    at.run()
    assert not at.exception
    assert _nav_context(at) == {}
    assert at.session_state["_ow_md_sel_ops_optimize"] == "1.0"
    assert at.session_state["ops_opt_live"] is True
    assert "First fix:" in " ".join(str(m.value) for m in at.markdown)
