"""UI/UX master list — Wave 2 orientation batch (C9 + C17).

Locks: C9 one-hop contextual return (origin captured on a cross-page jump,
offered only on the jump's destination, consumed on use, dropped on wander) ·
C17 data-derived verdict lines on Alerts (page level, from the uncapped counts)
and Security (decision queue, from the computed posture — which also makes that
section header's severity data-derived instead of static amber).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.core import state

_ROOT = Path(__file__).resolve().parents[1]


def _src(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


# ---- C9: nav_return_target semantics -----------------------------------------

def _with_session(monkeypatch, session):
    fake = SimpleNamespace(session_state=session, rerun=lambda: None)
    monkeypatch.setattr(state, "st", fake)
    return session


def test_return_target_offered_only_on_the_destination(monkeypatch):
    s = _with_session(monkeypatch, {
        "_ow_nav_origin": {"page": "Alerts", "section": "Open events", "dest": "Operations"},
    })
    hit = state.nav_return_target("Operations")
    assert hit == {"page": "Alerts", "section": "Open events", "dest": "Operations"}
    assert "_ow_nav_origin" in s                       # still armed until used


def test_wandering_off_the_destination_drops_the_origin(monkeypatch):
    s = _with_session(monkeypatch, {
        "_ow_nav_origin": {"page": "Alerts", "section": "Open events", "dest": "Operations"},
    })
    assert state.nav_return_target("Cost & Contract") is None
    assert "_ow_nav_origin" not in s                   # stale origin removed


def test_pop_and_garbage_origins(monkeypatch):
    s = _with_session(monkeypatch, {"_ow_nav_origin": {"page": "Alerts", "dest": "Operations"}})
    state.pop_nav_origin()
    assert "_ow_nav_origin" not in s
    _with_session(monkeypatch, {"_ow_nav_origin": "not-a-dict"})
    assert state.nav_return_target("Operations") is None
    _with_session(monkeypatch, {})
    assert state.nav_return_target("Operations") is None


def test_origin_capture_and_consume_are_wired():
    src = _src("app/core/state.py")
    # capture: cross-PAGE jumps only, stamped with the destination
    assert "capture_origin: bool = True" in src
    assert 'if capture_origin and page and _cur and page != _cur:' in src
    assert '"dest": page' in src
    # consume: persist the origin; a jump without one clears any stale origin
    assert 'st.session_state["_ow_nav_origin"] = dict(pending["origin"])' in src
    assert 'st.session_state.pop("_ow_nav_origin", None)' in src


def test_return_button_is_in_the_page_header_and_never_boomerangs():
    comp = _src("app/ui/components.py")
    assert 'key="ow_nav_return"' in comp
    assert "nav_return_target(title)" in comp
    # the return jump must not capture its own origin
    assert "capture_origin=False" in comp
    assert "pop_nav_origin()" in comp


# ---- C17: verdict lines ------------------------------------------------------

def test_alerts_page_verdict_is_data_derived_and_never_a_false_all_clear():
    src = _src("app/ui/pages/alerts.py")
    idx = src.index("page_verdict_line(page_verdict(")
    # rendered only when the uncapped counts resolved
    gate = src.rindex("if counts.usable():", 0, idx)
    # deferred-item: the crit signal now also carries the oldest-open-critical AGE
    # (built into _crit_label just under the gate), so a few more lines sit between
    # the gate and the verdict — but the gate still DIRECTLY guards it (no unguarded
    # all-clear path).
    assert src[gate:idx].count("\n") < 12
    assert 'Signal("bad", _crit_label)' in src
    assert '_crit_label = f"{crit_n} open critical(s)"' in src   # still crit-count-derived
    assert 'Signal("warn", f"{high_n} open high(s)")' in src
    # verdict renders BEFORE the section bar (the "before navigation" contract)
    assert idx < src.index('section = lazy_sections([')


def test_security_verdict_composes_from_posture_and_header_is_data_derived():
    src = _src("app/ui/security_center.py")
    assert "page_verdict_line(page_verdict([" in src
    assert '[p for p in posture if p.state == "Act"]' in src
    # the old static amber header is gone; severity comes from the same posture
    assert 'section_header("Security decision queue", "warn", "security")' not in src
    assert 'alarm_health(len(_act) or _open_n)' in src
    # the setup path keeps a neutral header
    assert 'section_header("Security decision queue", "", "security")' in src
