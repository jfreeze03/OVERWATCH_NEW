"""Regression locks for the round-3 bug hunt (v4.418.0)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.logic.formulas import safe_float

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- CCI-1: load_settings cache key includes the 'settings' DOMAIN salt ----------
def test_load_settings_key_tracks_the_settings_domain_salt(monkeypatch):
    from app.ui import components
    seen: list[str] = []
    monkeypatch.setattr(components, "_merged_settings_cached", lambda scope: seen.append(scope) or {})
    fake = SimpleNamespace(session_state={"_ow_refresh_salt": "g1",
                                          "_ow_domain_salts": {"settings": "s1"}})
    monkeypatch.setattr(components, "st", fake)
    components.load_settings("Admin")
    # a settings edit bumps ONLY the domain salt -> the key MUST change so the cache misses
    fake.session_state["_ow_domain_salts"]["settings"] = "s2"
    components.load_settings("Admin")
    assert seen[0] != seen[1], "settings edit must invalidate load_settings' cache"
    assert "settings:s1" in seen[0] and "settings:s2" in seen[1]


# --- bop-1: a real auto_suspend=0 (never-suspend) is preserved, not coerced to 600
def test_never_suspend_zero_is_preserved_in_whatif():
    # the fix drops `or 600`, so safe_float's default only fires on None/NaN/parse-error
    assert int(safe_float(0, 600)) == 0            # a real 0 survives
    assert int(safe_float(None, 600)) == 600       # missing -> default
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert 'get("auto_suspend"), 600) or 600)' not in opt   # the truthiness bug is gone
    assert 'live_suspend = int(safe_float(match.iloc[0].get("auto_suspend"), 600))' in opt


# --- DTE-1..4: data-derived strings are escaped before markdown ------------------
def test_markdown_sinks_escape_data_derived_text():
    admin = _src("app/ui/pages/admin.py")
    assert 'st.markdown(md_dollars(\n                    f"- **{name}** — {_age}. "' in admin
    alerts = _src("app/ui/pages/alerts.py")
    assert 'md_dollars(f"**[{row[\'SEVERITY\']}] {row[\'TITLE\']}**")' in alerts
    cost = _src("app/ui/pages/cost.py")
    assert "md_dollars(\n                    f\"**Untagged executions" in cost
    assert "from app.logic.formulas import format_usd, humanize_duration, md_dollars, safe_float" in cost
    cr = _src("app/ui/pages/control_room.py")
    assert 'str(anchor["LABEL"]).replace("`", "")' in cr   # backtick break-out closed


# --- wsk-1: the settings number_input clamps a below-floor stored value ----------
def test_settings_number_input_clamps_to_spec_bounds():
    admin = _src("app/ui/pages/admin.py")
    assert 'value = max(float(spec["min_value"]), value)' in admin
    assert 'value = min(float(spec["max_value"]), value)' in admin
