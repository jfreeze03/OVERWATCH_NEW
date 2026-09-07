"""Codex UI/UX review — adjudicated P1 fixes: kill constant/decorative section-header colour.

The v4.461-477 refactor neutralized constant-severity section headers so a header's colour is
data-derived (or neutral), never decoration. It MISSED a scatter of constant `"info"` (blue)
headers — 16 on Security plus brief/overview/decision_studio/security_center/workbench/spend —
and the Action Center header was a constant `"warn"` (amber on every render, incl. a clean/empty
queue). Blue/"info" and constant amber carry no severity meaning; the exception_summary / kpi_row /
alarm_health BELOW the header carry the real, data-derived state. These locks keep the class dead.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_UI_FILES = sorted((_ROOT / "app" / "ui").rglob("*.py"))


def test_no_constant_or_decorative_info_section_headers_app_wide():
    offenders: list[str] = []
    for f in _UI_FILES:
        src = f.read_text(encoding="utf-8")
        # a section_header whose severity literal is the constant "info" (renders blue)...
        offenders.extend(
            f"{f.name}: {m.group(0)[:72]}"
            for m in re.finditer(r'section_header\([^\n]*"info"', src))
        # ...or a header that falls back to blue on the non-exception branch
        if 'else "info"' in src:
            offenders.append(f'{f.name}: else "info"')
    assert not offenders, (
        "constant/decorative 'info' (blue) section headers must be neutral '' or data-derived: "
        + "; ".join(offenders))


def test_action_center_header_is_neutral_not_constant_amber():
    src = (_ROOT / "app" / "ui" / "workbench.py").read_text(encoding="utf-8")
    assert 'section_header("Action Center", "", "action")' in src
    assert 'section_header("Action Center", "warn"' not in src


def test_decision_studio_consumer_heading_uses_the_primitive():
    src = (_ROOT / "app" / "ui" / "decision_studio.py").read_text(encoding="utf-8")
    assert 'section_header("Cost per consumer & retirement candidates", "", "cost")' in src
    assert 'st.markdown("**Cost per consumer & retirement candidates**")' not in src
