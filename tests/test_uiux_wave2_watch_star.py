"""UI/UX master list — Wave 2 watch-affordance unification (F59).

"Watch" spoke three languages: a text button in Entity 360 (Watch/Unwatch), a raw
True/False column on the decision boards, and an 👁 eye badge on the Brief. F59
unifies them on ONE filled-star affordance: ★ = watched, everywhere. A cell/badge
shows ★ only when on (a column of hollow stars is noise); the interactive toggle
shows both states (★ Watching / ☆ Watch).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.ui import components

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_f59_star_helpers():
    assert components.watch_star(True) == "★"
    assert components.watch_star(False) == ""
    assert components.watch_star(1) == "★" and components.watch_star(0) == ""   # bool-coerced
    # a merge-miss NaN reads as NOT watched (bool(nan) is True — guarded)
    assert components.watch_star(float("nan")) == ""
    # bug-hunt r2: pd.NA must NOT crash (bool(pd.NA) raises "ambiguous"); NA/None/NaT
    # all read as NOT watched.
    assert components.watch_star(pd.NA) == ""
    assert components.watch_star(None) == "" and components.watch_star(pd.NaT) == ""
    assert components.watch_toggle_label(True) == "★ Watching"
    assert components.watch_toggle_label(False) == "☆ Watch"


def test_f59_decision_rows_renders_watched_as_a_star(monkeypatch):
    captured = {}
    monkeypatch.setattr(components, "selectable_table",
                        lambda view, **k: captured.update(view=view, cfg=k.get("column_config")))
    df = pd.DataFrame({"NEXT_MOVE": ["x", "y"], "QUERY_PREVIEW": ["a", "b"], "WATCHED": [True, False]})
    components.decision_rows(df, key="t", decision_col="NEXT_MOVE", why_col="QUERY_PREVIEW",
                             context_cols=("WATCHED",))
    # the raw booleans are gone — a filled star only on the watched row
    assert list(captured["view"]["WATCHED"]) == ["★", ""]
    assert "WATCHED" in (captured["cfg"] or {})


def test_f59_scenarios_table_stars_its_watched_column():
    studio = _src("app/ui/decision_studio.py")
    # the scenarios board maps WATCHED through the shared star + passes the config
    assert 'display["WATCHED"] = display["WATCHED"].map(watch_star)' in studio
    assert '_scen_cfg["WATCHED"] = watch_star_column()' in studio


def test_f59_entity_360_button_and_brief_badge_use_the_star():
    workbench = _src("app/ui/workbench.py")
    # Entity 360 toggle now reads its state as a star, not "Watch"/"Unwatch" text
    assert "watch_toggle_label(is_watched)" in workbench
    assert '"Unwatch" if is_watched else "Watch"' not in workbench
    # the Brief badge is a star, and the old eye emoji is gone from the watch surface
    badge = workbench.split("def render_watch_badge(", 1)[1].split("\ndef ", 1)[0]
    assert "★" in badge
    assert "👁" not in workbench            # no eye emoji anywhere in the file
