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


_LEGACY = {k: _TEMPLATE[k] for k in ("DIGEST_DATE", "MODEL", "CREATED_AT")} | {"BODY": "Spend was steady."}
_CHECKED_CLAUSE = "every figure is checked against those facts"


def _stub_digest(monkeypatch, row: dict) -> list[str]:
    """Serve ``row`` to every digest read on Brief and Overview; returns the run() source labels those reads
    passed (appended as the pages render)."""
    from app.ui.pages import brief, overview
    sources: list[str] = []

    def frame() -> QueryResult:
        return QueryResult(df=pd.DataFrame([row]), ok=True, source="stub")

    def run_stub(*args, **kwargs):
        sql = args[0] if args else kwargs.get("sql", "")
        if _is_digest(sql):
            sources.append(str(kwargs.get("source", "")))
            return frame()
        return _shaped_run(*args, **kwargs)

    def batch_stub(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        for spec in specs or []:
            if _is_digest(spec.get("sql")):
                sources.append(str(spec.get("source", "")))
                out[spec.get("key")] = frame()
        return out

    for mod in (brief, overview):
        monkeypatch.setattr(mod, "run", run_stub)
        if hasattr(mod, "run_batch"):
            monkeypatch.setattr(mod, "run_batch", batch_stub)
    return sources


def _database_at(monkeypatch, tip: int) -> None:
    """Model the connected database with V1..V<tip> applied (the harness default is the repo tip)."""
    import app.main as main_mod
    from app.ui import schema_gate

    def gate():
        schema_gate.remember(QueryResult(df=pd.DataFrame({"VERSION": list(range(1, tip + 1))}), ok=True))

    monkeypatch.setattr(main_mod, "_schema_floor_breach", gate)


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


# -- review r1 -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("page", ["Brief", "Overview"])
def test_unmatched_dollar_figures_are_escaped(monkeypatch, page):
    """W12: two UNGROUNDED dollar tokens must not pair into a LaTeX span (md_dollars at the sink)."""
    _stub_digest(monkeypatch, {**_TEMPLATE, "UNGROUNDED": "$12,345, $2,100"})
    at = _render(page)
    captions = [str(c.value) for c in at.caption]
    line = [c for c in captions if c.startswith("Unmatched figures in the withheld draft:")]
    assert line == ["Unmatched figures in the withheld draft: \\$12,345, \\$2,100"], captions


def test_before_v165_nothing_claims_the_figures_are_checked(monkeypatch):
    """W4 / W15: deployed before V165 is applied (the house order), the legacy read's row carries no grounding
    record; the Overview caption keeps its pre-4.602 wording and neither page's source claims the check."""
    _database_at(monkeypatch, 164)
    sources = _stub_digest(monkeypatch, _LEGACY)
    for page in ("Brief", "Overview"):
        at = _render(page)
        blob = " ".join(str(m.value) for m in at.markdown)
        assert "Figures not checked" in blob, page
        captions = " ".join(str(c.value) for c in at.caption)
        assert _CHECKED_CLAUSE not in captions, page
        assert "V165 is not applied yet" in captions, page
    ov_captions = [str(c.value) for c in at.caption]
    assert ("Written daily by TASK_DAILY_DIGEST from exec-board facts and alert counts only. Account-wide "
            "narrative — does not change with the company filter.") in ov_captions, ov_captions
    assert sources and set(sources) == {"DAILY_DIGEST (Cortex draft)"}, sources


def test_after_v165_a_pre_v165_row_still_does_not_claim_the_check(monkeypatch):
    """W4: the 07:20 row written before the apply stays up for up to a day; its chip says 'Figures not
    checked', so the caption must not say every figure is checked."""
    sources = _stub_digest(monkeypatch, {**_LEGACY, "BODY_SOURCE": None, "GROUNDING_OK": None})
    at = _render("Overview")
    captions = " ".join(str(c.value) for c in at.caption)
    assert _CHECKED_CLAUSE not in captions and "does not change with the company filter" in captions
    # the read itself is the V165 one (its source label may name the check: V165 is applied)
    assert set(sources) == {"DAILY_DIGEST (Cortex draft; figures checked against the exec board, "
                            "templated on mismatch)"}, sources


@pytest.mark.parametrize("row", [_AI, _TEMPLATE])
def test_after_v165_a_measured_row_states_the_check(monkeypatch, row):
    _stub_digest(monkeypatch, row)
    at = _render("Overview")
    captions = [str(c.value) for c in at.caption]
    want = ("Written daily by TASK_DAILY_DIGEST from exec-board facts and alert counts only; every figure is "
            "checked against those facts, and a templated digest is sent when any does not match. Account-wide "
            "narrative — does not change with the company filter.")
    assert want in captions, captions


# -- V171 (R1-228): the caption names the window and the scope, only for a row V171 wrote --------------------------

_WINDOW = ("for the 7 complete days to yesterday (warehouse compute spend; serverless, AI and storage not "
           "included)")
_V171_ROW = {**_AI, "FACTS": "WINDOW_DAYS=7; WAREHOUSE_SPEND_USD=12345.67; WAREHOUSE_CREDITS=3354.80; "
                             "OPEN_CRITICAL_ALERTS=0"}


@pytest.mark.parametrize("row", [_V171_ROW, {**_TEMPLATE, "FACTS": _V171_ROW["FACTS"]}])
def test_after_v171_a_v171_row_names_the_window_and_the_scope(monkeypatch, row):
    _stub_digest(monkeypatch, row)
    at = _render("Overview")
    captions = [str(c.value) for c in at.caption]
    want = ("Written daily by TASK_DAILY_DIGEST from exec-board facts " + _WINDOW + " and alert counts only; every "
            "figure is checked against those facts, and a templated digest is sent when any does not match. "
            "Account-wide narrative — does not change with the company filter.")
    assert want in captions, captions


def test_after_v171_a_pre_v171_row_does_not_claim_the_window(monkeypatch):
    """The 07:20 row written before the apply (V165 facts: today-inclusive, SPEND_USD) stays up for up to a day."""
    _stub_digest(monkeypatch, {**_AI, "FACTS": "WINDOW_DAYS=7; SPEND_USD=12345.67; CREDITS=3354.80"})
    at = _render("Overview")
    captions = " ".join(str(c.value) for c in at.caption)
    assert _CHECKED_CLAUSE in captions and _WINDOW not in captions and "7 complete days" not in captions


def test_before_v171_the_caption_keeps_its_pre_171_text(monkeypatch):
    """Deployed before V171 is applied (the house order): no window claim, even for a row shaped like V171's."""
    _database_at(monkeypatch, 170)
    _stub_digest(monkeypatch, _V171_ROW)
    at = _render("Overview")
    captions = [str(c.value) for c in at.caption]
    want = ("Written daily by TASK_DAILY_DIGEST from exec-board facts and alert counts only; every figure is "
            "checked against those facts, and a templated digest is sent when any does not match. Account-wide "
            "narrative — does not change with the company filter.")
    assert want in captions, captions
    assert not [c for c in captions if "7 complete days" in c]
