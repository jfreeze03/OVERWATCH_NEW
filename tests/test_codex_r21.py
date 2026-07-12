"""Codex r21 locks (2026-07-12). The AST fragment lock is the class-killer:
two panels claimed 'Fragment:' in their docstrings for months with no
@st.fragment decorator, so their sliders re-rendered the whole page."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _decorator_names(node):
    out = []
    for d in node.decorator_list:
        t = d.func if isinstance(d, ast.Call) else d
        out.append(t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", ""))
    return out


def test_fragment_docstrings_are_binding():
    offenders = []
    for path in (_ROOT / "app" / "ui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(node) or ""
            if doc.startswith("Fragment:") and "fragment" not in _decorator_names(node):
                offenders.append(f"{path.name}:{node.name}")
    assert not offenders, f"docstring says Fragment but no @st.fragment: {offenders}"


def test_recon_waits_for_the_toggle():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    gate = adm.split('key="adm_recon_on"', 1)
    assert len(gate) == 2                      # the toggle exists
    assert 'key="mart_recon"' in gate[1][:600]  # and the scan sits behind it


def test_settings_cache_key_carries_the_refresh_salt():
    cmp_src = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert '_settings_frame_cached(\n            f"global:{st.session_state.get(\'_ow_refresh_salt\', \'\')}")' in cmp_src


def test_query_param_writes_compare_first():
    st_src = (_ROOT / "app" / "core" / "state.py").read_text(encoding="utf-8")
    assert "if st.query_params.get(_PAGE_PARAM) != value:" in st_src
    cmp_src = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert 'if st.query_params.get("section") != _slug:' in cmp_src
