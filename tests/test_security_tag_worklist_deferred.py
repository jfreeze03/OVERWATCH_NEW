"""Perf (simulator finding): the Security default landing's object-tag governance panel no
longer auto-scans the untagged worklist. The selectbox used to default to the FIRST tag, so
untagged_objects (a tables-vs-tag-lineage read) fired on every Security landing; it's now
gated behind an explicit tag pick (the coverage table ranks which tag to drill). tag_cov
stays — it powers the coverage score/table, the landing's value. See
app/ui/pages/security.py::_tag_governance_panel. (perf audit 2026-09-02)
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _panel_body() -> str:
    src = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    return src.split("def _tag_governance_panel", 1)[1].split("\ndef ", 1)[0]


def test_untagged_worklist_is_gated_behind_an_explicit_pick():
    body = _panel_body()
    # a placeholder is the FIRST selectbox option (so nothing is auto-selected on landing)
    assert 'options=[_pick, *cov.df["TAG_NAME"]' in body
    # the untagged scan runs ONLY when a real tag is chosen
    assert 'if tag_choice and tag_choice != _pick:' in body
    gated = body.split("if tag_choice and tag_choice != _pick:", 1)[1]
    assert "security_sql.untagged_objects(" in gated          # the scan is inside the gate
    # the coverage score read (tag_cov) is NOT gated — it stays on the landing
    assert "security_sql.object_tag_coverage(" in body.split("if tag_choice", 1)[0]


st = pytest.importorskip("streamlit")
from packaging.version import parse as _parse_version  # noqa: E402

_APPTEST_OK = _parse_version(st.__version__) >= _parse_version("1.55.0")


@pytest.mark.skipif(not _APPTEST_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_security_landing_does_not_scan_the_untagged_worklist():
    import usage_sim
    report = usage_sim.simulate(pages=["Security"], scopes={"default": {}}, measure_rerun=False)
    assert not report["flows"][0]["error"], report["flows"][0]["error"]
    keys = {str(r.get("key")) for r in usage_sim._LEDGER if r.get("key")}
    assert not any(k.startswith("tag_untagged") for k in keys)   # deferred off the landing
    assert any(k.startswith("tag_cov") for k in keys)            # coverage score still served
