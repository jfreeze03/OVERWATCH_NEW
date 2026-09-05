"""Bug-hunt round 27: fresh angles over the UI/UX refactor (empty-vs-zero on folds,
session-state stickiness, hero/caption edges, r26-fix review). 1 unique confirmed
defect (reported by two dimensions); the rest refuted.

FALSE-ALL-CLEAR (r27 #1, confirmed): v4.474 data-drove the Control Room "Incidents"
section-header stripe with alarm_health(_open_now) — keyed ONLY off the open-incident
count. So it rendered a green "all-clear" stripe when open CRITICALS existed
(_open_crit>0, incidents 0 -> alarm_health(0)="ok") or when a feeding read FAILED
(collapsed to 0), directly contradicting the exception_summary below that flags exactly
those states (open criticals=bad, telemetry partial=warn). This is the r24
false-all-clear class, reintroduced. Fix: the header stripe is now derived from the
SAME exception set as the body (_inc_health) — partial telemetry -> neutral (never
green), any bad finding -> bad, any warn -> warn, only a fully-loaded genuinely-clean
state -> ok.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _incidents_block() -> str:
    cr = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
    return cr.split('elif section == "Incidents & triage":', 1)[1].split("\n    elif section ==", 1)[0]


def test_incidents_header_severity_mirrors_the_full_exception_set():
    inc = _incidents_block()
    # the open-incidents-ONLY alarm_health drive is gone (it caused the false green);
    # match the CALL form, not the bare token (a comment may still name it)
    assert 'section_header("Incidents", alarm_health(' not in inc
    # the stripe is derived from the same inputs as the exception list
    assert 'section_header("Incidents", _inc_health)' in inc
    assert "_partial = not (inc_met.usable() and _crit_known and _sv)" in inc
    assert '_inc_health = ""' in inc                              # partial telemetry -> neutral
    assert 'any(e["severity"] == "bad" for e in _exc)' in inc     # open crit/incident -> bad
    assert '_inc_health = "ok"' in inc                            # only the fully-clean state


def test_incidents_header_renders_after_the_exception_list_so_it_cannot_disagree():
    inc = _incidents_block()
    # build _exc first, derive the stripe from it, THEN render header + summary
    assert inc.index("_exc = []") < inc.index('section_header("Incidents", _inc_health)')
    assert inc.index('section_header("Incidents", _inc_health)') < inc.index("exception_summary(_exc")
    # partial-telemetry must gate to neutral BEFORE the ok branch (never a false green)
    assert inc.index("if _partial:\n            _inc_health") < inc.index('_inc_health = "ok"')
