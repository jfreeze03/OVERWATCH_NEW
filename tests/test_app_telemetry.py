"""#50 actionable app telemetry (v4.599): enriched APP_ERROR_LOG.CONTEXT, section visits, Ask demand.

Locks the contracts the Admin panels read back:
  * record_error writes `ref=… · viewer=… · v<build> · <context> · tb=…` within the 2000-char column,
    never raises, keeps the ref digest and the single tagged sink INSERT;
  * log_ui_event / _log_usage share ONE 6-column APP_USAGE prefix and UI rows never carry RENDER_MS
    (the only thing keeping them out of the first-paint p95);
  * section visits log once per page entry and once per change, never outside main();
  * the two Admin builders, their canaries, the zero-fill helper and the RUNBOOK disclosure.
Every test that reaches record_error stubs the sink's session, so no test can connect anywhere.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import APP_VERSION
from app.core import errors, session
from app.data import mart_sql
from app.logic import app_telemetry
from app.logic.navigate import PAGE_SECTION_LABELS
from tests._source import read

_ROOT = Path(__file__).resolve().parents[1]
_REF_RE = r"OW-\d{8}-\d{6}-[0-9A-F]{6}"
_USAGE_PREFIX = ("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE "
                 "(PAGE, SECTION, RENDER_MS, EVENT_KIND, IS_RERUN, USER_NAME) ")




def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


@pytest.fixture
def _err(monkeypatch):
    """record_error with an in-memory ring buffer and NO sink session (nothing can connect)."""
    monkeypatch.setattr(errors, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(session, "get_cached_session", lambda: None)

    def _last() -> dict:
        return errors.st.session_state[errors._BUFFER_KEY][-1]
    return _last


# ------------------------------------------------------------------ record_error CONTEXT ----

def test_record_error_context_carries_viewer_build_and_traceback(monkeypatch, _err):
    monkeypatch.setattr("app.core.identity.viewer_name", lambda: "JDOE")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        ref = errors.record_error("Admin", exc, context="page render")
    ctx = _err()["context"]
    assert re.fullmatch(_REF_RE, ref)
    assert ctx.startswith(f"ref={ref} · viewer=JDOE · v{APP_VERSION} · page render · tb=")
    assert "test_record_error_context_carries_viewer_build_and_traceback" in ctx   # the raising frame
    assert len(ctx) <= 2000


def test_record_error_viewer_is_sanitized_for_the_context_grammar(monkeypatch, _err):
    # a space or '·' in a name would break the ' · ' field grammar parse_error_context reads
    monkeypatch.setattr("app.core.identity.viewer_name", lambda: "J DOE·X")
    errors.record_error("Admin", ValueError("x"), context="c")
    assert " · viewer=J_DOE_X · " in _err()["context"]
    assert app_telemetry.parse_error_context(_err()["context"])["viewer"] == "J_DOE_X"


def test_record_error_context_fits_the_column_and_keeps_ref_and_tb(monkeypatch, _err):
    monkeypatch.setattr("app.core.identity.viewer_name", lambda: "JDOE")
    try:
        raise RuntimeError("long")
    except RuntimeError as exc:
        ref = errors.record_error("Admin", exc, context="x" * 5000)
    ctx = _err()["context"]
    assert len(ctx) <= 2000
    assert ctx.startswith(f"ref={ref} · viewer=JDOE · v{APP_VERSION} · xxx")
    assert " · tb=" in ctx and ctx.endswith("test_record_error_context_fits_the_column_and_keeps_ref_and_tb")
    parsed = app_telemetry.parse_error_context(ctx)
    assert parsed["ref"] == ref and parsed["tb"] and parsed["build"] == APP_VERSION


def test_record_error_survives_a_raising_viewer_lookup(monkeypatch, _err):
    def _boom() -> str:
        raise RuntimeError("st.user exploded")
    monkeypatch.setattr("app.core.identity.viewer_name", _boom)
    ref = errors.record_error("Admin", ValueError("x"), context="page render")
    assert re.fullmatch(_REF_RE, ref)
    assert f"ref={ref} · viewer=— · v{APP_VERSION} · page render" in _err()["context"]
    assert app_telemetry.parse_error_context(_err()["context"])["viewer"] == ""   # unknown, not '—'


def test_record_error_unraised_error_has_no_tb_and_no_NoneType(_err):
    # query._entitlement_refusal calls record_error OUTSIDE an except block: format_exc would say
    # 'NoneType: None'; the traceback tail reads error.__traceback__ instead and is simply absent.
    ref = errors.record_error("Security", PermissionError("refused"), context="execute_statement refused")
    ctx = _err()["context"]
    assert ctx.startswith(f"ref={ref} · viewer=")
    assert " · tb=" not in ctx and "NoneType" not in ctx

    class _Hostile(Exception):
        @property
        def __traceback__(self):          # not a traceback: extract_tb raises -> no tb, no raise
            return object()
    ref2 = errors.record_error("Security", _Hostile("h"), context="c")
    assert re.fullmatch(_REF_RE, ref2) and " · tb=" not in _err()["context"]


def test_record_error_digest_is_unchanged_by_the_enrichment(monkeypatch, _err):
    # the ref's hash half still keys on type|message|format_exc — the same error keeps the same ref
    monkeypatch.setattr("app.core.identity.viewer_name", lambda: "A")
    a = errors.record_error("P", ValueError("same"))
    monkeypatch.setattr("app.core.identity.viewer_name", lambda: "B")
    b = errors.record_error("P", ValueError("same"))
    assert a.split("-")[-1] == b.split("-")[-1]


def test_record_error_falls_back_when_the_context_builder_raises(monkeypatch, _err):
    def _boom(*a, **k):
        raise RuntimeError("builder bug")
    monkeypatch.setattr(errors, "_context_line", _boom)
    ref = errors.record_error("Admin", ValueError("x"), context="page render")
    assert _err()["context"] == f"ref={ref} · page render"


def test_error_sink_insert_carries_enriched_context(monkeypatch):
    from tests.test_statement_params import _Sis, _Stmt
    stmt = _Stmt()
    sis = _Sis(stmt)
    monkeypatch.setattr(errors, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(session, "get_cached_session", lambda: sis)
    errors.record_error("Security", RuntimeError("boom"), context="t")
    (sql,) = sis.sqls
    assert "APP_ERROR_LOG" in sql and "viewer=" in sql and f"v{APP_VERSION}" in sql
    assert "(PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)" in sql and "CURRENT_ROLE())" in sql
    (name, kw), = stmt.calls                     # still exactly ONE async, tagged INSERT
    assert name == "collect_nowait"
    assert kw["statement_params"] == {"QUERY_TAG": "OVERWATCH|page=Security|tier=write"}


def test_parse_error_context_round_trips():
    p = app_telemetry.parse_error_context
    new = f"ref=OW-20260928-101112-ABC123 · viewer=JDOE · v{APP_VERSION} · query key=x · tb=admin.py:12 f < query.py:3 run"
    assert p(new) == {"ref": "OW-20260928-101112-ABC123", "viewer": "JDOE", "build": APP_VERSION,
                      "context": "query key=x", "tb": "admin.py:12 f < query.py:3 run"}
    legacy = p("ref=OW-20260901-000000-00AA11 · page render")
    assert legacy == {"ref": "OW-20260901-000000-00AA11", "viewer": "", "build": "",
                      "context": "page render", "tb": ""}
    # the entitlement audit embeds '(viewer=A)'; the head's viewer wins, and a legacy row stays unknown
    ent_new = p(f"ref=OW-20260928-101112-ABC123 · viewer=JDOE · v{APP_VERSION} · "
                "execute_statement refused (viewer=JDOE): UPDATE t SET a = 1")
    assert ent_new["viewer"] == "JDOE" and ent_new["context"].startswith("execute_statement refused (viewer=")
    ent_old = p("ref=OW-20260901-000000-00AA11 · execute_statement refused (viewer=A): UPDATE t")
    assert ent_old["viewer"] == "" and ent_old["ref"] == "OW-20260901-000000-00AA11"
    proc = p("route R1 delivery failed: HTTP 500")          # server-proc rows never had a ref
    assert proc == {"ref": "", "viewer": "", "build": "", "context": "route R1 delivery failed: HTTP 500",
                    "tb": ""}
    bare = p("ref=OW-20260901-000000-00AA11")
    assert bare["ref"] and bare["context"] == ""
    for junk in (None, float("nan"), pd.NaT, "", "   ", 42):
        out = p(junk)
        assert set(out) == {"ref", "viewer", "build", "context", "tb"}


# ---------------------------------------------------------------------------- APP_USAGE ----

def _capture_buffer(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr("app.core.query._buffer_write",
                        lambda prefix, row, **kw: sent.append((prefix, row, kw)))
    monkeypatch.setattr("app.core.identity.identity_sql", lambda: "CURRENT_USER()")
    return sent


def test_log_ui_event_writes_section_or_null_and_never_render_ms(monkeypatch):
    import streamlit

    from app.ui import components
    sent = _capture_buffer(monkeypatch)
    state: dict = {}
    monkeypatch.setattr(streamlit, "session_state", state)
    components.log_ui_event("section_visit", page="Operations", section="Tasks")
    components.log_ui_event("csv_export", page="Security")
    components.log_ui_event("k" * 60, page="", section="s" * 200)
    assert [s[0] for s in sent] == [_USAGE_PREFIX] * 3
    assert sent[0][1] == "SELECT 'Operations', 'Tasks', NULL, 'section_visit', FALSE, CURRENT_USER()"
    assert sent[1][1] == "SELECT 'Security', NULL, NULL, 'csv_export', FALSE, CURRENT_USER()"
    # PAGE falls back to the kind (cut to 80), SECTION is cut to 80, EVENT_KIND to 40
    assert sent[2][1] == (f"SELECT '{'k' * 60}', '{'s' * 80}', NULL, '{'k' * 40}', FALSE, CURRENT_USER()")
    assert sent[0][2] == {"off_flag": "_ow_usage_off", "downgrade_flag": "_ow_usage_oldshape"}
    # the RENDER_MS slot is a literal NULL — a UI event cannot carry a render time at all
    assert "render_ms" not in inspect.signature(components.log_ui_event).parameters
    for flag in ("_ow_usage_off", "_ow_usage_oldshape"):
        state.clear()
        state[flag] = True
        components.log_ui_event("section_visit", page="Operations", section="Tasks")
    assert len(sent) == 3                          # both flags short-circuit before enqueueing


def test_usage_prefix_is_identical_in_main_and_components(monkeypatch):
    import app.main as m
    from app.core.query import _statement_allowed
    from app.ui import components
    pat = r'"(INSERT INTO DBA_MAINT_DB\.OVERWATCH\.APP_USAGE \([^)]*\) )"'
    comp = set(re.findall(pat, read("app/ui/components.py")))
    main = set(re.findall(pat, read("app/main.py")))
    assert comp == {_USAGE_PREFIX}
    assert main == {_USAGE_PREFIX, "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS) "}
    # runtime: page visit + section visit + UI event land in ONE buffer group (byte-identical prefix)
    sent = _capture_buffer(monkeypatch)
    state: dict = {}
    monkeypatch.setattr(m, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(m, "_buffer_write", lambda prefix, row, **kw: sent.append((prefix, row, kw)))
    monkeypatch.setattr(m, "identity_sql", lambda: "CURRENT_USER()")
    import streamlit
    monkeypatch.setattr(streamlit, "session_state", state)
    m._log_usage("Operations", 1234)
    components.log_ui_event("section_visit", page="Operations", section="Tasks")
    components.log_ui_event("alert_ack", page="Alerts")
    assert {s[0] for s in sent} == {_USAGE_PREFIX}
    assert sent[0][1] == "SELECT 'Operations', NULL, 1234, 'page_visit', FALSE, CURRENT_USER()"
    # a first row with NULL SECTION and a later row with text still form one allowed statement
    stmt = _USAGE_PREFIX + " UNION ALL ".join(s[1] for s in sent)
    ok, why = _statement_allowed(stmt)
    assert ok, why
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(stmt, dialect="snowflake")


def test_mark_page_entry_moves_only_on_a_page_change(monkeypatch):
    import app.main as m
    state: dict = {"_ow_run_seq": 7}
    monkeypatch.setattr(m, "st", SimpleNamespace(session_state=state))
    m._mark_page_entry("Operations")
    assert state["_ow_entry_page"] == "Operations" and state["_ow_page_entry"] == 7
    state["_ow_run_seq"] = 8                       # a same-page rerun (widget change / st.rerun())
    m._mark_page_entry("Operations")
    assert state["_ow_page_entry"] == 7
    state["_ow_run_seq"] = 9
    m._mark_page_entry("Alerts")
    assert state["_ow_entry_page"] == "Alerts" and state["_ow_page_entry"] == 9


def _section_harness(monkeypatch, state: dict) -> list:
    from app.ui import components
    calls: list = []
    monkeypatch.setattr(components, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(components, "log_ui_event",
                        lambda kind, page="", section="": calls.append((kind, page, section)))
    return calls


def test_section_visit_logs_once_per_page_entry_and_per_change(monkeypatch):
    from app.ui.components import _log_section_visit
    state: dict = {"_ow_entry_page": "Operations", "_ow_page_entry": 5}
    calls = _section_harness(monkeypatch, state)
    for label in ("Tasks", "Tasks", "Warehouses", "Tasks"):
        _log_section_visit("ops_section", label, nested=False)
    assert len(calls) == 3
    state["_ow_page_entry"] = 6                    # left and came back: a new page entry
    _log_section_visit("ops_section", "Tasks", nested=False)
    assert len(calls) == 4
    assert all(c[0] == "section_visit" and c[1] == "Operations" for c in calls)
    assert [c[2] for c in calls] == ["Tasks", "Warehouses", "Tasks", "Tasks"]
    assert not any(str(k).startswith("_ow_nav_") for k in state)   # state.py purges that prefix


def test_nested_visit_names_parent_and_relogs_under_new_parent(monkeypatch):
    from app.ui.components import _log_section_visit
    state: dict = {"_ow_entry_page": "Operations", "_ow_page_entry": 5, "ops_section": "Tasks"}
    calls = _section_harness(monkeypatch, state)
    _log_section_visit("ops_task_view", "Health", nested=True)
    _log_section_visit("ops_task_view", "Health", nested=True)
    assert calls == [("subsection_visit", "Operations", "Tasks ▸ Health")]
    state["ops_section"] = "Warehouses"             # same sub-view label under a changed parent
    _log_section_visit("ops_task_view", "Health", nested=True)
    assert calls[-1] == ("subsection_visit", "Operations", "Warehouses ▸ Health")
    state["_ow_entry_page"] = "Brief"              # a page with no section key: the bare label
    state["_ow_page_entry"] = 9
    _log_section_visit("x_view", "Detail", nested=True)
    assert calls[-1] == ("subsection_visit", "Brief", "Detail")


def test_section_visit_without_page_or_with_raising_state_is_silent(monkeypatch):
    from app.ui import components
    from app.ui.components import _log_section_visit
    calls = _section_harness(monkeypatch, {})
    _log_section_visit("ops_section", "Tasks", nested=False)   # not rendered through main()
    assert calls == []

    class _Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("session gone")
    monkeypatch.setattr(components, "st", SimpleNamespace(session_state=_Boom()))
    _log_section_visit("ops_section", "Tasks", nested=False)   # must not raise

    def _raising_logger(*a, **k):
        raise RuntimeError("buffer gone")
    monkeypatch.setattr(components, "st", SimpleNamespace(
        session_state={"_ow_entry_page": "Operations", "_ow_page_entry": 1}))
    monkeypatch.setattr(components, "log_ui_event", _raising_logger)
    _log_section_visit("ops_section", "Tasks", nested=False)   # must not raise


def test_lazy_sections_and_main_wire_the_logger():
    comp = read("app/ui/components.py")
    body = _body(comp, "lazy_sections")
    call = body.index("_log_section_visit(key, str(choice), nested=not deep_link)")
    assert body.index("choice = st.segmented_control(") < call
    assert call < body.index("\n    if deep_link:\n")
    assert 'sentinel = f"_ow_secvis_{key}"' in _body(comp, "_log_section_visit")   # not the purged _ow_nav_ prefix
    main = _body(read("app/main.py"), "main")
    mark = main.index("_mark_page_entry(page)")
    assert main.index('_schema_floor_breach() if page != "Admin" else None') < mark
    assert mark < main.index("_RENDERERS[page]()")


def test_new_usage_rows_cannot_pollute_first_paint_p95():
    assert "RENDER_MS IS NOT NULL" in mart_sql.app_performance_slo(7)
    v017 = read("snowflake/migrations/V017__hardening_v7.sql")
    arm = v017.split("APPROX_PERCENTILE(RENDER_MS, 0.95)", 1)[1].split("GROUP BY", 1)[0]
    assert "AND RENDER_MS IS NOT NULL" in arm
    assert "COALESCE(EVENT_KIND, 'page_visit') = 'page_visit'" in mart_sql.app_usage_summary(30)
    # every APP_USAGE writer in the app: only _log_usage's page_visit row may carry a render time
    writers = [p for p in (_ROOT / "app").rglob("*.py")
               if "OVERWATCH.APP_USAGE (" in p.read_text(encoding="utf-8")]
    assert sorted(p.relative_to(_ROOT).as_posix() for p in writers) == ["app/main.py", "app/ui/components.py"]
    assert "{sql_literal(str(section)[:80]) if section else 'NULL'}, NULL, " in _body(
        read("app/ui/components.py"), "log_ui_event")


# --------------------------------------------------------------------- Admin builders ----

def _canaries() -> dict:
    from app.data.canary import CANARIES
    return dict(CANARIES)


def test_section_visit_summary_builder():
    sql = mart_sql.section_visit_summary(90)
    assert "EVENT_KIND IN ('section_visit', 'subsection_visit')" in sql
    assert "SECTION IS NOT NULL" in sql and "MIN(MIN(AT)) OVER () AS FIRST_LOGGED_AT" in sql
    assert "VISITS_30D" in sql and "IFF(EVENT_KIND = 'subsection_visit', 'Sub-view', 'Section') AS LEVEL" in sql
    assert "DATEADD('day', -90," in sql and "LIMIT" not in sql
    big = mart_sql.section_visit_summary(9999)
    assert "DATEADD('day', -365," in big and "-9999" not in big
    assert "VISITS_7D" in mart_sql.section_visit_summary(7)
    assert "ACCOUNT_USAGE" not in sql and "CURRENT_USER()" not in sql
    assert _canaries()["mart.section_visit_summary"]() == mart_sql.section_visit_summary(1)
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, dialect="snowflake")


def test_ask_demand_summary_builder():
    sql = mart_sql.ask_demand_summary(90)
    assert "EVENT_KIND IN ('ask_refused', 'ask_answered', 'ask_failed')" in sql
    assert "SUM(COUNT(*)) OVER () AS ALL_ASKS" in sql
    assert "SUM(IFF(EVENT_KIND = 'ask_refused', COUNT(*), 0)) OVER () AS REFUSED_TOTAL" in sql
    assert "SECTION AS ASK_TEXT" in sql and sql.rstrip().endswith("LIMIT 100")
    assert mart_sql.ask_demand_summary(90, limit=10**6).rstrip().endswith("LIMIT 500")
    assert mart_sql.ask_demand_summary(90, limit=0).rstrip().endswith("LIMIT 1")
    assert "DATEADD('day', -365," in mart_sql.ask_demand_summary(10**6)
    assert "ACCOUNT_USAGE" not in sql and "CURRENT_USER()" not in sql
    assert _canaries()["mart.ask_demand_summary"]() == mart_sql.ask_demand_summary(1)
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, dialect="snowflake")


def test_with_unvisited_sections_fills_zero_rows_and_flags_retired():
    labels = {"Operations": ["Queries", "Tasks", "Warehouses"], "Admin": ["Settings", "Performance"]}
    df = pd.DataFrame({
        "PAGE": ["Operations", "Operations", "Operations", "Admin"],
        "LEVEL": ["Section", "Sub-view", "Section", "Section"],
        "SECTION": ["Tasks", "Tasks ▸ Health", "Old name", "Performance"],
        "VISITS_30D": [3.0, 1.0, 1.0, 2.0], "VISITS": [9.0, 2.0, 1.0, 4.0], "USERS": [2.0, 1.0, 1.0, 1.0],
        "LAST_VISIT_AT": pd.to_datetime(["2026-09-20"] * 4),
        "FIRST_LOGGED_AT": pd.to_datetime(["2026-09-01"] * 4),
    })
    before = df.copy(deep=True)
    out = app_telemetry.with_unvisited_sections(df, labels)
    pd.testing.assert_frame_equal(df, before)                  # input never mutated
    rows = {(r.PAGE, r.LEVEL, r.SECTION): r for r in out.itertuples()}
    for page, sec in (("Operations", "Queries"), ("Operations", "Warehouses"), ("Admin", "Settings")):
        z = rows[(page, "Section", sec)]
        assert z.VISITS == 0 and z.VISITS_30D == 0 and z.USERS == 0
        assert pd.isna(z.LAST_VISIT_AT)
        assert pd.Timestamp(z.FIRST_LOGGED_AT) == pd.Timestamp("2026-09-01")
    assert ("Operations", "Section (retired label)", "Old name") in rows
    assert ("Operations", "Sub-view", "Tasks ▸ Health") in rows       # sub-views are never zero-filled/retired
    assert len(out) == 4 + 3
    # page order from the mapping, then level, then visits (most first)
    assert list(out["PAGE"]) == ["Operations"] * 5 + ["Admin"] * 2
    assert list(out["SECTION"]) == ["Tasks", "Queries", "Warehouses", "Tasks ▸ Health", "Old name",
                                    "Performance", "Settings"]      # zero rows keep label order (stable)
    assert list(out["LEVEL"])[:5] == ["Section", "Section", "Section", "Sub-view", "Section (retired label)"]
    # shaped harness frames type the text columns as float: cast, never raise
    shaped = pd.DataFrame({"PAGE": [1.0], "LEVEL": [2.0], "SECTION": [3.0], "VISITS": [1.0],
                           "USERS": [1.0], "LAST_VISIT_AT": [1.0], "FIRST_LOGGED_AT": [1.0]})
    shaped_before = shaped.copy(deep=True)
    s_out = app_telemetry.with_unvisited_sections(shaped, PAGE_SECTION_LABELS)
    pd.testing.assert_frame_equal(shaped, shaped_before)
    assert len(s_out) == 1 + sum(len(v) for v in PAGE_SECTION_LABELS.values())
    # empty / shapeless frames come back as an unchanged copy
    empty = pd.DataFrame(columns=["PAGE", "SECTION"])
    assert app_telemetry.with_unvisited_sections(empty, labels).empty
    odd = pd.DataFrame({"X": [1]})
    odd_out = app_telemetry.with_unvisited_sections(odd, labels)
    pd.testing.assert_frame_equal(odd_out, odd)
    assert odd_out is not odd


# ------------------------------------------------------------------------- Admin wiring ----

def test_admin_usage_panels_wired_after_page_adoption_and_never_import_ask():
    admin = read("app/ui/pages/admin.py")
    perf = _body(admin, "_performance_tab")
    assert perf.index('section_header("Page adoption (30d)"') < perf.index("_usage_detail_panels()")
    assert perf.index("_usage_detail_panels()") < perf.index("Fleet slow/failed fetches")
    panels = _body(admin, "_usage_detail_panels")
    assert "mart_sql.section_visit_summary(90)" in panels and "mart_sql.ask_demand_summary(90)" in panels
    assert panels.count('tier="recent"') == 2
    assert "app_telemetry.with_unvisited_sections(sv.df, PAGE_SECTION_LABELS)" in panels
    assert '"REFUSED_TOTAL", "ALL_ASKS"' in panels and "safe_float(" in panels
    for banned in ("st.info(", "st.success(", "methodology_note(", "st.dataframe("):
        assert banned not in panels, banned
    # the documented Ask revert (delete app/logic/ask + the page) must leave Admin and the mart intact
    for rel in ("app/ui/pages/admin.py", "app/data/mart_sql.py", "app/logic/app_telemetry.py"):
        assert "app.logic.ask" not in read(rel), rel


def test_admin_error_families_show_viewers_and_build():
    obs = _body(read("app/ui/pages/admin.py"), "_observability_tab")
    assert "app_telemetry.parse_error_context" in obs
    assert 'VIEWERS=("VIEWER", "nunique")' in obs and 'LAST_BUILD=("BUILD", "first")' in obs
    assert 'FIRST_SEEN=("LOGGED_AT", "min")' in obs
    assert 'grouped["VIEWERS"].where(grouped["VIEWERS"] > 0)' in obs
    assert "newest 100 logged errors" in obs


def test_runbook_discloses_section_ask_and_error_viewer_telemetry():
    rb = read("RUNBOOK.md")
    block = rb.split("**Usage analytics disclosure:**", 1)[1].split("\n\n", 1)[0]
    for needle in ("section_visit", "subsection_visit", "ask_answered", "ask_failed", "ask_refused",
                   "8-word", "digits masked", "APP_ERROR_LOG.CONTEXT", "viewer name", "app build",
                   "traceback", "ERROR_LOG_RETENTION_DAYS", "APP_USAGE_RETENTION_DAYS", "auditors will ask"):
        assert needle in block, needle


def test_sub_view_relogs_when_its_parent_section_is_visited_again(monkeypatch):
    """Review r1: Tasks -> Warehouses -> Tasks re-logs 'Tasks ▸ Health' exactly as it re-logs 'Tasks'
    (the nested token carries the page's section-visit sequence)."""
    from app.ui.components import _log_section_visit
    state: dict = {"_ow_entry_page": "Operations", "_ow_page_entry": 5, "ops_section": "Tasks"}
    calls = _section_harness(monkeypatch, state)
    _log_section_visit("ops_section", "Tasks", nested=False)
    _log_section_visit("ops_task_view", "Health", nested=True)
    _log_section_visit("ops_task_view", "Health", nested=True)          # a rerun: no re-log
    state["ops_section"] = "Warehouses"
    _log_section_visit("ops_section", "Warehouses", nested=False)
    state["ops_section"] = "Tasks"
    _log_section_visit("ops_section", "Tasks", nested=False)
    _log_section_visit("ops_task_view", "Health", nested=True)          # the parent was visited again
    assert [c[2] for c in calls] == ["Tasks", "Tasks ▸ Health", "Warehouses", "Tasks", "Tasks ▸ Health"]


def test_failed_usage_flush_logs_the_insert_head_not_the_row_values(monkeypatch):
    """Review r1: a failed telemetry flush used to copy 200 chars of the INSERT (viewer name, section
    labels, refused-question stems) into APP_ERROR_LOG.CONTEXT; now only the target, columns and row count."""
    from app.core import query
    sql = (_USAGE_PREFIX + "SELECT 'Ask', 'who is jane doe', NULL, 'ask_refused', FALSE, 'JDOE'"
           " UNION ALL SELECT 'Ask', NULL, 120, 'page_visit', FALSE, 'JDOE'")
    head = query._async_sql_head(sql)
    assert head.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, SECTION, RENDER_MS")
    assert "jane" not in head and "JDOE" not in head and "(2 row(s); values not logged)" in head
    seen: list = []
    monkeypatch.setattr(query, "record_error", lambda page, exc, context="": seen.append(context))
    monkeypatch.setattr(query, "get_session", lambda: (_ for _ in ()).throw(RuntimeError("no session")))
    assert query.execute_statement_async(sql, page="Ask") is False
    assert seen and "jane" not in seen[-1] and seen[-1].startswith("execute_statement_async: INSERT INTO")
