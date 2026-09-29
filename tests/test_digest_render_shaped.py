"""V165 (Next-Fifty #24): the Brief and Overview digest expanders render every provenance branch.

tests/test_pages_shaped.py paints both pages with shaped data, but a shaped BODY_SOURCE is a placeholder string,
so only the "not measured" branch runs there. This module reuses that harness (its autouse stub fixture is
imported below) and swaps in real DAILY_DIGEST rows: a TEMPLATE sent on a mismatch (chip, unmatched-figure
caption, the withheld-draft popover, no model in the Overview title) and an AI-written body whose figures all
matched (the model stays in the Overview title).
"""

from __future__ import annotations

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402
from tests.test_pages_shaped import (  # noqa: E402, F401  (_stub_shaped: the harness's autouse stub fixture)
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_run,
    _stub_shaped,
)

_TEMPLATE = {"DIGEST_DATE": "2026-09-30", "MODEL": "llama3.1-8b", "CREATED_AT": "2026-09-30 07:20",
             "BODY": "Templated digest (not AI-written): the AI draft stated figures that do not match the "
                     "exec-board facts, so OVERWATCH sent the facts directly.",
             "BODY_SOURCE": "TEMPLATE", "GROUNDING_OK": False, "FIGURES_CHECKED": 5,
             "UNGROUNDED": "2 critical, 15%", "AI_BODY": "Spend rose 15% and 2 critical alerts are open.",
             "FACTS": "WINDOW_DAYS=7; OPEN_CRITICAL_ALERTS=0"}
_AI = {**_TEMPLATE, "BODY": "Spend was $12,345.67 over 7 days.", "BODY_SOURCE": "AI", "GROUNDING_OK": True,
       "FIGURES_CHECKED": 2, "UNGROUNDED": None, "AI_BODY": "Spend was $12,345.67 over 7 days."}


def _is_digest(sql) -> bool:
    s = str(sql or "")
    return "DAILY_DIGEST" in s and "DIGEST_DATE" in s


def _stub_digest(monkeypatch, row: dict) -> None:
    from app.ui.pages import brief, overview

    def frame() -> QueryResult:
        return QueryResult(df=pd.DataFrame([row]), ok=True, source="stub")

    def run_stub(*args, **kwargs):
        sql = args[0] if args else kwargs.get("sql", "")
        return frame() if _is_digest(sql) else _shaped_run(*args, **kwargs)

    def batch_stub(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        for spec in specs or []:
            if _is_digest(spec.get("sql")):
                out[spec.get("key")] = frame()
        return out

    for mod in (brief, overview):
        monkeypatch.setattr(mod, "run", run_stub)
        if hasattr(mod, "run_batch"):
            monkeypatch.setattr(mod, "run_batch", batch_stub)


def _render(page: str) -> AppTest:
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, page)
    at.run()
    assert not at.exception, f"{page}: {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error), page
    return at


def _popover_labels(at: AppTest) -> list[str]:
    """AppTest has no popover accessor: a popover is a generic Block of type 'popover'."""
    labels, stack = [], [at._tree]
    while stack:
        node = stack.pop()
        if getattr(node, "type", "") == "popover":
            labels.append(str(node.proto.popover.label))
        stack.extend(getattr(node, "children", {}).values())
    return labels


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page", ["Brief", "Overview"])
def test_a_templated_digest_says_so_and_offers_the_withheld_draft(monkeypatch, page):
    _stub_digest(monkeypatch, _TEMPLATE)
    at = _render(page)
    labels = [str(e.label) for e in at.expander]
    title = [lbl for lbl in labels if "digest" in lbl.lower() or "narrative" in lbl.lower()]
    assert title == ["Morning digest (templated, not AI-written) — 2026-09-30"], labels   # no model: not AI-written
    blob = " ".join(str(m.value) for m in at.markdown)
    assert "Templated, not AI-written: the AI draft stated figures that are not in the exec-board facts" in blob
    assert "Templated digest (not AI-written)" in blob                                     # the body that was sent
    captions = " ".join(str(c.value) for c in at.caption)
    assert "Unmatched figures in the withheld draft: 2 critical, 15%" in captions
    assert "Show the withheld AI draft" in _popover_labels(at)


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page, want", [
    ("Brief", "AI morning narrative — 2026-09-30"),
    ("Overview", "AI morning narrative — 2026-09-30 (llama3.1-8b)"),
])
def test_an_ai_digest_shows_the_measured_match(monkeypatch, page, want):
    _stub_digest(monkeypatch, _AI)
    at = _render(page)
    labels = [str(e.label) for e in at.expander]
    assert want in labels, labels
    blob = " ".join(str(m.value) for m in at.markdown)
    assert "AI-written; all 2 figures match the exec-board facts" in blob
    assert "Show the withheld AI draft" not in _popover_labels(at)
