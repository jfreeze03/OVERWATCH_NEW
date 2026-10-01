"""R1-118: Cost ▸ Spend ▸ Compute pools & notebooks — a FAILED notebook-runtime read is not "no rows".

The per-user pool drill gated on ``notebooks.usable()`` (ok AND non-empty), so a timeout or privilege error on
NOTEBOOKS_CONTAINER_RUNTIME_HISTORY told the operator the feed "has no rows for this window" -- a false
statement about the data, a few lines above the red "Query failed" that the Notebook subset guard drew for the
same read. Drives the real page under the shaped harness (tests/test_pages_shaped.py), replacing only the
notebook read and the pool-table selection.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.result import QueryResult

st = pytest.importorskip("streamlit")
from tests.test_pages_shaped import (  # noqa: E402,F401  (autouse fixture: shaped reads everywhere)
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_run,
    _stub_shaped,
)

_NO_ROWS = "has no rows for this window"


def _drill(monkeypatch, notebooks: QueryResult):
    from streamlit.testing.v1 import AppTest

    from app.ui.pages.cost_parts import spend

    def _run(*a, **k):
        if str(k.get("key", "")).startswith("spcs_notebooks_"):
            return notebooks
        return _shaped_run(*a, **k)

    real_select = spend.selectable_table

    def _select(df, key, **k):     # the pool row is "clicked"; every other table keeps its real widget
        return 0 if key == "spcs_pool_sel" else real_select(df, key, **k)

    monkeypatch.setattr(spend, "run", _run)
    monkeypatch.setattr(spend, "selectable_table", _select)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Cost Intelligence")
    at.session_state["cost_service_attribution_detail"] = True
    at.session_state["cost_service_detail_section"] = "Compute pools & notebooks"
    at.run()
    assert not at.exception, at.exception
    assert any("Users driving" in str(m.value) for m in at.markdown), "the pool drill did not render"
    return at


def _text(elements) -> str:
    return " | ".join(str(e.value) for e in elements)


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("kind", ["timeout", "missing_column", "other"])
def test_failed_notebook_read_is_not_reported_as_no_rows(monkeypatch, kind):
    at = _drill(monkeypatch, QueryResult(ok=False, error=f"boom ({kind})", error_kind=kind))
    every = _text(at.info) + _text(at.caption) + _text(at.error)
    assert _NO_ROWS not in every
    assert "the notebook-runtime read (NOTEBOOKS_CONTAINER_RUNTIME_HISTORY) failed" in _text(at.error)


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_unreadable_notebook_view_is_needs_setup(monkeypatch):
    at = _drill(monkeypatch, QueryResult(ok=False, error="boom (privilege)", error_kind="privilege"))
    assert "NOTEBOOKS_CONTAINER_RUNTIME_HISTORY, which isn't readable" in _text(at.info)
    assert _NO_ROWS not in _text(at.info) + _text(at.caption)


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_an_empty_notebook_feed_still_says_no_rows(monkeypatch):
    at = _drill(monkeypatch, QueryResult(df=pd.DataFrame(), ok=True, source="stub"))
    assert _NO_ROWS in _text(at.caption)                 # quiet no_data_yet, not a blue info banner
    assert _NO_ROWS not in _text(at.info) and "failed" not in _text(at.error)


def _ordered(at, *types: str) -> list[tuple[str, str]]:
    """Main-area elements of the given types in render order (Block.__iter__ is depth-first)."""
    return [(e.type, str(getattr(e, "value", ""))) for e in at.main if e.type in types]


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("kind", ["timeout", "other"])
def test_failed_notebook_error_sits_under_the_notebook_subset_heading(monkeypatch, kind):
    """R1-118 follow-up: the drill's 'failed' line points the reader to the error 'under Notebook subset
    below', but that heading rendered only on a SUCCESSFUL read -- on a failure the page showed a bare
    'Query failed' with no heading. The pointer must land: drill line, then the heading, then the error."""
    at = _drill(monkeypatch, QueryResult(ok=False, error=f"boom ({kind})", error_kind=kind))
    order = _ordered(at, "markdown", "error")

    def _at(pred) -> int:
        hits = [i for i, (t, v) in enumerate(order) if pred(t, v)]
        assert hits, order
        return hits[0]

    i_drill = _at(lambda t, v: t == "error" and "failed, so per-user cost is unknown" in v)
    i_head = _at(lambda t, v: t == "markdown" and "Notebook subset" in v)
    i_err = _at(lambda t, v: t == "error" and f"Query failed: boom ({kind})" in v)
    assert "under Notebook subset below" in order[i_drill][1]
    assert i_drill < i_head < i_err, order
