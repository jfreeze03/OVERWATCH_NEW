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


def test_effect_line_offers_clear_owner_and_defer_via_v092_flags():
    # V092 gave SP_ACTION_LIFECYCLE explicit P_CLEAR_OWNER / P_CLEAR_DEFER, so
    # blanking a set owner and toggling a set defer OFF are real savable effects
    # again (v4.318 had dropped them while the proc could only COALESCE-keep). An
    # undated row still must not be sent a fabricated today+7 due.
    wb = _src("app/ui/workbench.py")
    block = wb.split("def _render_action_detail", 1)[1].split("\ndef ", 1)[0]
    # clear signals derived from the diff: a SET owner blanked / a SET defer off
    assert "_clear_owner = bool(_cur_owner) and not owner.strip()" in block
    assert "_clear_defer = _cur_defer_on and not defer_on" in block
    # the effect line names each clear; assign/defer are now the else-branch
    assert '"unassign the owner"' in block
    assert '"clear the defer (resume now)"' in block
    assert 'elif owner.strip() and owner.strip() != _cur_owner:' in block
    # the flags are threaded into the write
    assert "clear_owner=_clear_owner," in block and "clear_defer=_clear_defer," in block
    # unchanged: null-aware due (an undated row is never sent a fabricated today+7)
    assert "_due_arg = due if (_had_due or _due_changed) else None" in block
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
