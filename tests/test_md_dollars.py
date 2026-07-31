"""$-in-markdown regression locks.

Streamlit's markdown treats a bare '$'...'$' pair as a LaTeX math span, so any caption
holding two dollar tokens — two format_usd() amounts, an amount + 'USER$', two literal
rates — renders the text between them in serif-italic math (live screenshots 2026-07-11
and 2026-07-31). The house rule: wrap the WHOLE string in formulas.md_dollars() at the
markdown sink. The AST scan below fails CI when a new sink call carries 2+ dollar tokens
without the wrapper, so the bug class cannot silently return.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# markdown-rendering Streamlit sinks (positional arg 0 is the rendered string).
# st.expander/toggle LABELS render markdown+LaTeX too (Streamlit >= 1.28).
_SINKS = {"caption", "markdown", "info", "warning", "error", "success", "expander"}


def test_md_dollars_escapes_only_dollars():
    from app.logic.formulas import format_usd, md_dollars
    assert md_dollars("a $9 vs $300 case") == "a \\$9 vs \\$300 case"
    assert md_dollars(format_usd(1967.79)) == "\\$1,967.79"
    assert md_dollars("no dollars here") == "no dollars here"
    assert "**bold**" in md_dollars("**bold** $1")          # other markdown untouched


def _dollar_tokens(node: ast.AST) -> int:
    """Count $ characters in constant strings + format_usd() calls under `node` —
    each contributes a '$' to the rendered string."""
    count = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            count += sub.value.count("$")
        elif isinstance(sub, ast.Call):
            fn = sub.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name == "format_usd":
                count += 1
    return count


def _is_md_dollars_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
    return name == "md_dollars"


def test_no_unescaped_dollar_pairs_in_markdown_sinks():
    offenders: list[str] = []
    for path in sorted((_ROOT / "app" / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _SINKS
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "st"
                    and node.args):
                continue
            arg = node.args[0]
            if _is_md_dollars_call(arg):
                continue                       # escaped at the sink — safe
            if _dollar_tokens(arg) >= 2:       # two $ tokens WILL pair into a math span
                rel = path.relative_to(_ROOT).as_posix()
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "Streamlit markdown sink(s) render 2+ '$' tokens in one string — they pair into a "
        "LaTeX math span (serif-italic font bug). Wrap the whole string in "
        f"formulas.md_dollars(...): {offenders}"
    )


def test_house_sinks_escape_dollars():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    # result_caption + panel_help escape at the sink so note/help prose is always safe
    assert 'st.caption(md_dollars(" · ".join(bits)))' in comp
    assert "st.markdown(md_dollars(text))" in comp
    ai = (_ROOT / "app" / "ui" / "ai_panel.py").read_text(encoding="utf-8")
    assert "st.markdown(md_dollars(answer))" in ai           # LLM output is data
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert ".replace('$', chr(92)" not in ov                 # the ad-hoc patch is retired
