"""UI/UX master list — Wave 2 batch 6: C25 empty-state conversion + F56 actions.

Locks: the shared vocabulary gained 'unavailable' (red lead + detail expander)
and an optional next-best-action; guard() routes BOTH its branches through
empty_state (empty defaults to the quiet no_data_yet caption — a successful
zero-row read is not a setup prompt — with kind="clean" for verified-good
empties); 131 raw st.info/st.success absences across the UI now speak the
vocabulary; the workflow empties carry doorways (Watchlist → Browse the
catalog, Experiments → Open Action Center).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_vocabulary_has_all_four_kinds_and_the_action_doorway():
    comp = _src("app/ui/components.py")
    body = comp.split("def empty_state(", 1)[1].split("\ndef ", 1)[0]
    assert 'kind == "clean"' in body and "exception_summary([], message)" in body
    assert 'kind == "needs_setup"' in body
    assert 'kind == "unavailable"' in body          # C25: the read itself failed
    assert 'st.expander("Error detail")' in body    # full error one click away
    assert "action_label" in body and "on_action" in body   # F56 doorway
    assert "st.button(" in body


def test_guard_routes_both_branches_through_the_vocabulary():
    comp = _src("app/ui/components.py")
    body = comp.split("def guard(", 1)[1].split("\ndef ", 1)[0]
    # empty branch: quiet caption by default, kind= for verified-clean callers;
    # review fix: a successful read proves setup exists, so a setup hint is
    # suppressed under a verified-clean green row (always a contradiction)
    assert 'kind: str = "no_data_yet"' in comp.split("def guard(", 1)[1].split(")", 1)[0]
    assert 'hint="" if kind == "clean" else setup_hint' in body
    # error branch: 'unavailable' with the detail expander; setup absence stays calm
    assert 'empty_state("unavailable"' in body
    assert 'empty_state("needs_setup"' in body
    # the old raw renderings are gone from guard
    assert "st.info(empty_message)" not in body


def test_the_sweep_left_no_raw_absence_regression_hotspots():
    # the heavy pages converted their absences — a hard ceiling on raw
    # st.info/st.success so new absences must go through the vocabulary
    # (receipts and context notes legitimately remain)
    ceilings = {
        # every swept file pinned at its post-sweep count — the sanctioned
        # leftovers are receipts, verdict renders, and structural notes
        "app/ui/pages/operations.py": 2,     # graph-picker instruction + filter-scope note
        "app/ui/pages/security.py": 0,
        "app/ui/pages/control_room.py": 1,   # triage-inputs-incomplete note
        "app/ui/pages/cost_parts/optimize.py": 4,   # simulate/engine verdicts + no-reads note
        "app/ui/pages/alerts.py": 8,         # F51 receipt + F50 verdict renders
        "app/ui/pages/brief.py": 0,
        "app/ui/pages/overview.py": 0,
        "app/ui/pages/admin.py": 0,
        "app/ui/workbench.py": 1,            # blast-radius safety caveat (deliberate info)
        "app/ui/decision_studio.py": 0,
        "app/ui/pages/ask.py": 2,            # answer headlines (direct answers, not absences)
        "app/ui/pages/cost.py": 1,           # since-last-visit status opener
        "app/ui/pages/cost_parts/spend.py": 3,      # SPCS structural notes
        "app/ui/pages/cost_parts/contract.py": 2,   # rate-context notes
        "app/ui/pages/cost_parts/compare.py": 0,
        "app/ui/pages/cost_parts/unit_costs.py": 0,
        "app/ui/pages/cost_parts/ai_chargeback.py": 1,   # queue receipt
    }
    for rel, cap in ceilings.items():
        raw = len(re.findall(r"st\.(?:info|success)\(", _src(rel)))
        assert raw <= cap, f"{rel}: {raw} raw st.info/st.success > ceiling {cap}"


def test_f56_workflow_empties_are_doorways():
    wb = _src("app/ui/workbench.py")
    assert 'action_label="Browse the catalog"' in wb
    assert 'action_key="es_watchlist_browse"' in wb
    ds = _src("app/ui/decision_studio.py")
    assert 'action_label="Open Action Center"' in ds
    assert 'action_key="es_experiments_create"' in ds


def test_verified_clean_guards_say_so():
    # spot-pins: guard callers whose empty IS the good outcome pass kind="clean"
    ops = _src("app/ui/pages/operations.py")
    assert ops.count('kind="clean"') >= 3
    comp = _src("app/ui/pages/cost_parts/compare.py")
    assert 'kind="clean"' in comp


def test_docs_carry_the_new_contracts():
    arch = _src("ARCHITECTURE.md")
    assert "Empty/absent-state vocabulary (C25)" in arch
    assert "Operator-write seam (C48)" in arch
    claude_md = _src("CLAUDE.md")
    assert "empty_state" in claude_md and "write_gate_open" in claude_md
    agents = _src("AGENTS.md")
    assert "empty_state" in agents and "write_gate_open" in agents
