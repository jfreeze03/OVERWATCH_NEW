"""Files the deployed app reads at runtime must be in snowflake.yml's artifacts (R1-275 / R1-329).

Alerts > Native delivery reads BOTH snowflake/ templates from the deployed tree, but `snow streamlit
deploy` uploads only the listed artifacts. snowflake/webhook_delivery.sql (added with V007, after the
artifact list was written) was never listed, so on SiS the Slack / Teams template -- the delivery path
this account uses -- always rendered 'File not found in this deployment'.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _artifacts() -> list[str]:
    """The `artifacts:` list of snowflake.yml (no YAML dependency: one `- path` per line)."""
    lines = (_ROOT / "snowflake.yml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "artifacts:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = []
    for ln in lines[start + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        m = re.match(r"\s*-\s+(\S+)\s*$", ln)
        if m:
            out.append(m.group(1))
    return out


def _alerts_template_files() -> list[str]:
    """The filenames of the `for filename, blurb in ((...), ...)` template loop on Alerts."""
    tree = ast.parse((_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Tuple)
                and [getattr(e, "id", None) for e in node.target.elts] == ["filename", "blurb"]):
            return [pair.elts[0].value for pair in node.iter.elts]
    raise AssertionError("the Alerts native-delivery template loop moved -- re-point this lock")


def _shipped(rel: str, artifacts: list[str]) -> bool:
    return any(rel == a or (a.endswith("/") and rel.startswith(a)) for a in artifacts)


def test_every_runtime_template_is_a_deploy_artifact():
    artifacts = _artifacts()
    files = _alerts_template_files()
    assert files == ["native_alert_templates.sql", "webhook_delivery.sql"]
    missing = [f for f in files if not _shipped(f"snowflake/{f}", artifacts)]
    assert not missing, f"snowflake.yml artifacts do not ship {missing}; the deployed page says 'File not found'"
    for f in files:
        assert (_ROOT / "snowflake" / f).is_file(), f


def test_artifact_list_shape():
    assert _artifacts()[:3] == ["streamlit_app.py", "environment.yml", "app/"]
    for rel in _artifacts():
        assert (_ROOT / rel.rstrip("/")).exists(), f"snowflake.yml lists a missing artifact: {rel}"
