"""UI/UX master list — Wave 2 write polish (F54 + F58).

Locks: F54 dirty-checks the Action Center "Save work item" — a no-op save
(nothing changed, no comment) can't fire, so it stops writing empty audit
rows · F58 a plain-English effect line above the write's SQL preview names
exactly what the write does (status move, owner/due change, comment; the
experiment save names its ledger consequence).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_action_save_is_dirty_checked_with_an_effect_line():
    wb = _src("app/ui/workbench.py")
    block = wb.split("def _render_action_detail", 1)[1].split("\ndef ", 1)[0]
    # the effect line is the diff of the form against the current row
    assert "_effects: list[str] = []" in block
    assert 'f"set status {current_status} → {status}"' in block
    assert '"add a comment"' in block
    # F54: emptiness of the diff IS the dirty check — Save is disabled with nothing to save
    assert "_dirty = bool(_effects)" in block
    assert 'disabled=not _dirty' in block
    assert "No changes to save yet" in block
    # F58: the effect line renders above the SQL preview
    assert '"This will "' in block
    assert block.index("_dirty = bool(_effects)") < block.index('st.expander("SQL preview")')


def test_effect_line_only_claims_what_the_proc_actually_does():
    # review fix: SP_ACTION_LIFECYCLE uses COALESCE-keep semantics, so a blank
    # owner / toggled-off defer are no-ops it can't perform, and an undated row
    # must not be sent a fabricated today+7 due. The line must not promise those.
    wb = _src("app/ui/workbench.py")
    block = wb.split("def _render_action_detail", 1)[1].split("\ndef ", 1)[0]
    assert 'if owner.strip() and owner.strip() != _cur_owner:' in block   # blank = no-op, not "unassign"
    assert "owner → " not in block                                         # the old unassign phrasing is gone
    assert "{owner.strip() or 'unassigned'}" not in block
    assert "clear the defer" not in block                                  # SP can't null the defer
    assert "_due_arg = due if (_had_due or _due_changed) else None" in block  # null-aware due
    assert "due_date=_due_arg," in block


def test_experiment_effect_is_honest_about_status_and_audit():
    ds = _src("app/ui/decision_studio.py")
    block = ds.split("def _render_experiment_detail", 1)[1].split("\ndef ", 1)[0]
    assert "savings ledger" in block                       # F58 example: book $X to the ledger
    assert "reverse its prior ledger booking" in block     # the reversal branch
    # review fix: status clause only on a real move; "audited" only on the settle path
    assert "if update_status != current:" in block
    assert '_settle = update_status in ("VERIFIED", "REJECTED", "ROLLED_BACK")' in block
    assert '" — audited." if _settle else "."' in block
    assert block.index("_parts") < block.index('st.expander("SQL preview")')
