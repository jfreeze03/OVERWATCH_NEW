"""Locks for bug-hunt round 12 (wf_a45db231-7dd): self-review of the v4.573-v4.575 perf+UX diff.

Six LOW/cosmetic defects INTRODUCED by those waves — 4 double row-select affordances (nav-primitive
default hint doubling with an existing caption), the Overview trend-note keyed on the wrong flag, and
the remediation toast's unconditional "booked" claim — all pinned so a regression re-fails here.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_double_caption_sites_suppress_the_default_hint():
    # the 4 sites that carry their OWN drill caption pass hint="" so the v4.575 default doesn't double
    ov = _read("app/ui/pages/overview.py")
    assert 'key="ov_actions_sel"' in ov and 'hint="",' in ov.split('key="ov_actions_sel"', 1)[1][:300]
    cr = _read("app/ui/pages/control_room.py")
    assert 'key="cr_triage_sel"' in cr and 'hint="")' in cr.split('key="cr_triage_sel"', 1)[1][:220]
    ops = _read("app/ui/pages/operations.py")
    assert 'entity_type="WAREHOUSE", hint="", column_config={' in ops   # concurrency-peaks
    assert 'entity_type="WAREHOUSE", hint="")' in ops                    # queue-per-warehouse pressure board


def test_overview_trend_note_reflects_served_leg_not_using_mart():
    ov = _read("app/ui/pages/overview.py")
    assert '_served_live = "WAREHOUSE_METERING_HISTORY" in str(getattr(trend_source, "source", "") or "")' in ov
    # the note is now keyed on _served_live, not the exec_board-only using_mart flag
    assert 'note="mart-first" if using_mart else "live fallback' not in ov


def test_remediation_toast_only_says_booked_when_booked():
    opt = _read("app/ui/pages/cost_parts/optimize.py")
    assert '(" and booked." if _book_ledger' in opt       # _book_ledger = the ledger INSERT's own gate
    assert 'executed and booked."' not in opt   # the unconditional claim is gone


def test_overview_still_within_account_usage_budget():
    # the trend-note fix must not add an ACCOUNT_USAGE literal (overview budget is 1)
    assert _read("app/ui/pages/overview.py").count("ACCOUNT_USAGE") <= 1
