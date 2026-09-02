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
    from app.ui import ai_panel, components, security_center, workbench
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

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_SYSADMINS")
    # The v4.431 startup schema gate reads SCHEMA_VERSION; a shaped VERSION column would
    # look below the floor and block every page with the migration banner. This harness
    # tests page BODIES, so bypass the gate (it has its own dedicated tests).
    monkeypatch.setattr(main_mod, "_schema_floor_breach", lambda: None)

    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"

    for module in (main_mod, components, ai_panel, ds_render, security_center, workbench,
                   overview, control_room, cost, operations, alerts, security, admin, brief,
                   ask, decision_studio, ai_chargeback, compare, contract, optimize, spend,
                   unit_costs):
        for fname, fstub in _READ_STUBS.items():
            if hasattr(module, fname):
                monkeypatch.setattr(module, fname, fstub)
        if hasattr(module, "current_role"):
            monkeypatch.setattr(module, "current_role", lambda: "SNOW_SYSADMINS")
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))


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
