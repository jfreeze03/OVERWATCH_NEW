"""UI/UX master list — C19: Operator vs Audit presentation modes.

Locks: the mode is a profile pref (PRESENT_MODE in USER_PREFS) hydrated into
_ow_present_mode like density, toggled in the sidebar and persisted via the
recovered upsert MERGE; operator (default) keeps the daily surface lean while
audit shows the full evidence chain. The gate is one seam — result_caption
always shows the SOURCE (the app's ethos) but trims the per-panel fetched-at
stamp + methodology note in operator mode — plus the audit_mode()/methodology
helpers gating the how-computed / reconciliation / backtest expanders.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_mode_helpers_default_to_operator():
    comp = _src("app/ui/components.py")
    pm = comp.split("def present_mode(", 1)[1].split("\ndef ", 1)[0]
    # unset OR anything but "audit" -> operator (a fresh viewer gets the lean surface)
    assert 'st.session_state.get("_ow_present_mode") == "audit"' in pm
    assert '"audit" if' in pm and '"operator"' in pm
    am = comp.split("def audit_mode(", 1)[1].split("\ndef ", 1)[0]
    assert 'present_mode() == "audit"' in am
    mn = comp.split("def methodology_note(", 1)[1].split("\ndef ", 1)[0]
    assert "audit_mode()" in mn                          # note shows in audit only


def test_result_caption_keeps_source_trims_the_rest_in_operator():
    comp = _src("app/ui/components.py")
    body = comp.split("def result_caption(", 1)[1].split("\ndef ", 1)[0]
    assert "_audit = audit_mode()" in body
    assert 'bits.append(f"Source: {result.source}")' in body   # source is unconditional
    # fetched-at and the note are gated to audit mode
    assert "if result.fetched_at and _audit:" in body
    assert "if note and _audit:" in body


def test_pref_plumbing_recovered_with_the_mode_key():
    prefs = _src("app/data/prefs_sql.py")
    assert "PRESENT_MODE" in prefs                        # allowlisted key
    assert "def upsert_pref_sql(" in prefs and "MERGE INTO" in prefs
    assert "_valid_key(key)" in prefs                     # allowlist-gated
    main = _src("app/main.py")
    # hydrated like density, from USER_PREFS
    assert 'if str(r["PREF_KEY"]) == "PRESENT_MODE"' in main
    assert 'if mode_pref in ("operator", "audit"):' in main


def test_sidebar_toggle_persists_only_on_a_real_flip():
    main = _src("app/main.py")
    assert '"Audit detail", key="_ow_present_mode_toggle", on_change=_on_present_toggle' in main
    # review fix: persist via on_change (fires ONLY on a genuine user flip),
    # never by diffing a render-body read against present_mode() — that latched
    # a pre-hydrate 'operator' and overwrote a saved 'audit' pref
    assert "def _on_present_toggle()" in main
    assert 'prefs_sql.upsert_pref_sql("PRESENT_MODE", _m)' in main
    assert "if _new_mode != present_mode():" not in main    # the destructive diff is gone
    # the hydrate seeds the toggle key so a late 'audit' hydration flips it
    assert 'st.session_state["_ow_present_mode_toggle"] = (mode_pref == "audit")' in main


def test_docs_carry_the_presentation_mode_contract():
    arch = _src("ARCHITECTURE.md")
    assert "Presentation mode (C19)" in arch and "present_mode()" in arch
    assert "audit_mode()" in _src("CLAUDE.md")


def test_methodology_expanders_are_audit_gated():
    for rel, marker in (
        ("app/ui/pages/cost_parts/spend.py", "Why totals differ across pages"),
        ("app/ui/pages/cost_parts/spend.py", "Cost coverage ladder"),
        ("app/ui/pages/overview.py", "Forecast accuracy"),
        ("app/ui/pages/admin.py", "Object-cost ledger reconciliation"),
    ):
        src = _src(rel)
        idx = src.index(marker)
        before = src[max(0, idx - 200):idx]
        assert "if audit_mode():" in before, f"{rel}: {marker}"
