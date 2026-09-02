"""Production-shaped render contracts (review #2).

The empty-stub smoke (test_pages_apptest.py) exercises only the honest-EMPTY branches;
the populated branches that index specific columns (df["CREDITS_BILLED"], iloc[0][...])
never run, so a renamed/missing column raising KeyError in a populated table/chart passes
CI. This harness stubs run() to return a small, correctly-SHAPED frame instead of an
empty one — columns parsed from each builder's own SELECT, dtypes chosen by the same
name convention the app formats on — so the populated branches actually render and a
column contract break surfaces as a test failure.

Teeth: the shaped frame carries EXACTLY the builder's SELECT columns, so a page that
indexes a column its builder does not return raises KeyError here. Scope: this shapes
the direct run() reads in the page modules; run_batch / run_mart_first reads still return
empty (a follow-up), so this is a coverage FLOOR, not yet every populated branch.
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


def _shaped_run(*args, **kwargs):
    sql = args[0] if args else kwargs.get("sql", "")
    cols = _columns_for(str(sql))
    if not cols:
        return QueryResult(df=pd.DataFrame(), ok=True, source=str(kwargs.get("source", "stub")))
    df = pd.DataFrame({c: [_col_value(c, r) for r in range(2)] for c in cols})
    return QueryResult(df=df, ok=True, source=str(kwargs.get("source", "stub")))


def _fake_execute(*_args, **_kwargs):
    return True, "stubbed"


@pytest.fixture(autouse=True)
def _stub_shaped(monkeypatch):
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, components
    from app.ui.pages import admin, alerts, control_room, cost, operations, overview, security
    from app.ui.pages.cost_parts import ai_chargeback, contract, optimize, spend

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_SYSADMINS")
    monkeypatch.setattr(main_mod, "run", _shaped_run)

    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))

    for module in (overview, control_room, cost, operations, alerts, security, admin,
                   spend, contract, ai_chargeback, optimize):
        if hasattr(module, "run"):
            monkeypatch.setattr(module, "run", _shaped_run)
        if hasattr(module, "execute_statement"):
            monkeypatch.setattr(module, "execute_statement", _fake_execute)
        if hasattr(module, "current_role"):
            monkeypatch.setattr(module, "current_role", lambda: "SNOW_SYSADMINS")
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))
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


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page", _SHAPED_PAGES)
def test_pages_render_with_shaped_data(page):
    at = AppTest.from_function(_entry, default_timeout=25)
    at.run()
    assert not at.exception
    _nav_to(at, page)
    at.run()
    assert not at.exception, f"{page} (shaped): {at.exception}"
    assert at.title or at.markdown, page
