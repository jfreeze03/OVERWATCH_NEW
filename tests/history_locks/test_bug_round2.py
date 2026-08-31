"""Locks for bug round 2 fixes (docs/reviews/BUG_ROUND_2_2026-07-29.md)."""
from datetime import timedelta
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]


def test_b4_anomaly_scorers_drop_todays_partial_row():
    """B4: today's still-growing FACT_WAREHOUSE_DAILY row must not be scored — a
    steady warehouse's part-day spend false-flags a HIGH anomaly every morning."""
    from app.logic.anomaly import complete_days_only
    from app.logic.formulas import account_today
    today = account_today()
    df = pd.DataFrame({"DAY": [today - timedelta(days=3), today - timedelta(days=1), today],
                       "USD": [10.0, 10.0, 0.3]})
    out = complete_days_only(df)
    assert len(out) == 2 and out["DAY"].max() < today          # today dropped, history kept
    for rel in ("app/ui/pages/control_room.py", "app/ui/pages/cost_parts/spend.py",
                "app/ui/pages/operations.py"):
        assert "complete_days_only(" in (_ROOT / rel).read_text(encoding="utf-8"), rel


def _state_fn(name: str) -> str:
    st = (_ROOT / "app" / "core" / "state.py").read_text(encoding="utf-8")
    return st.split(f"def {name}", 1)[1].split("\ndef ", 1)[0]


def test_b6_jump_box_cleared_only_when_a_jump_is_consumed():
    """B6: the _ow_jump reset must run AFTER the no-pending early return — the old
    unconditional clear erased the user's pick on the rerun that delivered it."""
    body = _state_fn("consume_pending_navigation")
    assert "if not pending:\n        return" in body
    assert body.index('pop("_ow_nav_pending"') < body.index('"_ow_jump"] = None')


def test_b7_db_validation_uses_live_classification():
    """B7: validate the DB pin with the same live-inventory rules the picker uses,
    not the static databases_for() tuples (which wiped DBA_MAINT_DB / new DBs)."""
    body = _state_fn("init_filters")
    assert "classify_databases(" in body
    assert "databases_for(" not in body


def test_b8_cross_page_nav_clamped_to_profile():
    """B8: never route a viewer to a page their profile does not offer.

    v4.374.0: the clamp keys on active_profile (the VIEWER's profile under
    owner's-rights SiS), not resolve_role_profile (the owner's role for everyone)."""
    body = _state_fn("consume_pending_navigation")
    assert "PAGES_BY_PROFILE" in body and "active_profile" in body
    assert 'page = "Overview"' in body                          # universal fallback
