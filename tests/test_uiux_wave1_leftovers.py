"""UI/UX master list — Wave 1 leftovers (C23, C13, C24, F31).

Locks: C23 section-header severity derives from the section's own data via
alarm_health (amber only with findings, green when verified-clean, neutral when
unresolved) · C13 the filter-contract banner renders only when a sharp active
filter is not fully applied (quiet caption otherwise) · C24 verified-clean renders
as the compact ok-row, not a full-width success banner · F31 routine row-cap
truncation is a quiet caption, not a yellow alarm.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.core.result import QueryResult
from app.ui import components
from app.ui.components import alarm_health

_ROOT = Path(__file__).resolve().parents[1]


def _src(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


# ---- C23: alarm_health -------------------------------------------------------

def test_alarm_health_from_query_results():
    rows = QueryResult(df=pd.DataFrame({"A": [1]}), ok=True)
    clean = QueryResult(df=pd.DataFrame(), ok=True)
    failed = QueryResult(ok=False, error="boom")
    assert alarm_health(rows) == "warn"      # findings -> amber
    assert alarm_health(clean) == "ok"       # verified clean -> green
    assert alarm_health(failed) == ""        # unresolved -> neutral, never all-clear


def test_alarm_health_from_counts():
    assert alarm_health(3) == "warn"
    assert alarm_health(0) == "ok"
    assert alarm_health(None) == ""          # unknown count is neutral
    assert alarm_health("garbage") == ""     # never raises


def test_alarm_health_wired_at_the_static_amber_sites():
    ops = _src("app/ui/pages/operations.py")
    sec = _src("app/ui/pages/security.py")
    assert ops.count("alarm_health(") >= 7   # failures/RCA/copy/drift/SLA/streaks/freshness/monitors
    assert sec.count("alarm_health(") >= 2   # MFA gaps + single-factor
    assert "alarm_health(unm)" in _src("app/ui/pages/cost.py")      # unmapped entities
    assert "alarm_health(_crit_n)" in _src("app/ui/pages/brief.py")  # Fires
    # dangerous control surfaces stay DELIBERATELY amber
    assert ops.count("# C23: deliberately amber") == 2


# ---- C13: contract banner only on misread risk -------------------------------

def _fake_st(markdown, captions):
    return SimpleNamespace(
        markdown=lambda value, **_k: markdown.append(str(value)),
        caption=lambda value: captions.append(str(value)),
    )


def test_contract_banner_only_when_a_sharp_filter_is_at_risk(monkeypatch):
    markdown: list[str] = []
    captions: list[str] = []
    monkeypatch.setattr(components, "st", _fake_st(markdown, captions))

    # an ACTIVE database filter this section ignores -> the blue banner
    components.section_filter_contract(
        {"company": "ALFA", "days": 7, "database": "MYDB"},
        applies=("company", "days"))
    assert any("ow-filter-contract" in m for m in markdown)

    markdown.clear(), captions.clear()
    # every active dim fully applied -> quiet caption, no banner
    components.section_filter_contract(
        {"company": "ALFA", "days": 7, "database": "MYDB"},
        applies=("company", "days", "database"))
    assert not markdown and captions and "Applies:" in captions[0]

    markdown.clear(), captions.clear()
    # only company/days active (they always carry a value) -> caption, no banner
    components.section_filter_contract(
        {"company": "ALFA", "days": 7}, applies=())
    assert not markdown and captions


def test_contract_partial_active_dim_still_banners(monkeypatch):
    markdown: list[str] = []
    captions: list[str] = []
    monkeypatch.setattr(components, "st", _fake_st(markdown, captions))
    # a panel-dependent ACTIVE dim is a misread risk -> banner
    components.section_filter_contract(
        {"company": "ALFA", "days": 7, "warehouse_contains": "WH_"},
        applies=("company", "days"), partial=("warehouse_contains",))
    assert any("ow-filter-contract" in m for m in markdown)


# ---- C24: verified-clean is a compact row ------------------------------------

def test_clean_empty_state_is_a_compact_ok_row(monkeypatch):
    markdown: list[str] = []
    captions: list[str] = []
    successes: list[str] = []
    fake = SimpleNamespace(
        markdown=lambda value, **_k: markdown.append(str(value)),
        caption=lambda value: captions.append(str(value)),
        success=lambda value, **_k: successes.append(str(value)),
        info=lambda value, **_k: None,
    )
    monkeypatch.setattr(components, "st", fake)
    components.empty_state("clean", "Everything verified clean.", hint="a hint")
    assert not successes                                  # no full-width banner
    assert any("ow-exception--ok" in m for m in markdown)  # the compact row
    assert any("Everything verified clean." in m for m in markdown)
    assert captions == ["a hint"]


# ---- F31: truncation is a caption, not an alarm ------------------------------

def test_truncation_renders_quietly(monkeypatch):
    captions: list[str] = []
    warnings: list[str] = []
    fake = SimpleNamespace(
        caption=lambda value: captions.append(str(value)),
        warning=lambda value, **_k: warnings.append(str(value)),
        info=lambda value, **_k: None,
        error=lambda value, **_k: None,
    )
    monkeypatch.setattr(components, "st", fake)
    res = QueryResult(df=pd.DataFrame({"A": [1, 2, 3]}), ok=True, truncated=True)
    assert components.guard(res, "empty") is True
    assert not warnings
    assert any("Showing the first" in c for c in captions)
