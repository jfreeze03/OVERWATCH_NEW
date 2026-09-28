"""Shared source readers for the shape-pinning tests (Next-Fifty #49).

Import as ``from tests._source import ...`` -- the one supported spelling (pytest.ini puts the repo root
on sys.path; tests/ has a conftest, the Snowflake-session guard, but no ``__init__``).

``read(rel)`` is the one-liner ~180 test files each re-define as ``_src``/``_read``/``_source``.
``page_source(name)`` reads a page the way a reader of the page sees it: ``app/ui/pages/<name>.py``
plus every module of the parts package that page owns, so a lock survives a section moving between
the shell and its parts. ``migration_tip()`` derives the repo tip, and ``changelog_entry(version)``
returns one CHANGELOG section by its heading, never "the top entry" (the top moves every release).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "app" / "ui" / "pages"
# The parts package a page OWNS. Not derivable from imports: brief.py and overview.py also import
# cost_parts.contract, and operations' package is ops_parts (not operations_parts).
# tests/test_release_lockstep.py fails if a *_parts package appears that no page claims.
PAGE_PARTS = {"cost": "cost_parts", "operations": "ops_parts"}


def read(rel: str) -> str:
    """A repo file's text, by repo-relative path."""
    return (ROOT / rel).read_text(encoding="utf-8")


def page_files(name: str) -> list[Path]:
    """The page shell first, then its parts modules in name order (no ``__init__``)."""
    shell = PAGES / f"{name}.py"
    if not shell.is_file():
        raise FileNotFoundError(f"no page app/ui/pages/{name}.py")
    files = [shell]
    parts = PAGE_PARTS.get(name)
    if parts:
        files += sorted(p for p in (PAGES / parts).glob("*.py") if p.name != "__init__.py")
    return files


def page_source(name: str) -> str:
    """The page shell plus its parts package, newline-joined."""
    return "\n".join(p.read_text(encoding="utf-8") for p in page_files(name))


def migration_tip() -> int:
    """The highest VNNN in snowflake/migrations (the repo tip)."""
    return max(int(m.group(1)) for p in (ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql")
               if (m := re.match(r"V(\d+)__", p.name)))


def changelog_entry(version: str) -> str:
    """The ``## <version> - ...`` CHANGELOG section, heading line included, up to the next ``## ``.

    Older headings use an em-dash separator (``## 4.1.0 — ...``), so both are accepted."""
    m = re.search(rf"^## {re.escape(version)} [-—] .*?(?=^## |\Z)", read("CHANGELOG.md"), re.M | re.S)
    if not m:
        raise AssertionError(f"CHANGELOG.md has no '## {version} - ' entry")
    return m.group(0)
