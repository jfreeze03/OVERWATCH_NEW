"""The agent instruction files point only at paths that exist (R1-273 / R1-306).

CLAUDE.md is auto-loaded every session and AGENTS.md sends every other agent to it, so a pointer in either
one is followed without a second look. Until v4.607 both routed agents to
docs/handoff/CODE_HANDOFF_2026-07-14.md as "session state" / "the source of truth" long after it went stale
(V048, HEAD d2b4085, a bundle regenerator that globbed V0*.sql); that file is gone, and current state comes
from git log, the top CHANGELOG entry and APP_VERSION. This lock keeps a deleted or renamed doc, test or
module from leaving a dangling pointer behind: every backticked repo path in either file must resolve.
Placeholders (gen_vNNN, test_vNNN_*, Vnnn, <PROC>) are patterns, not paths, and are skipped; a plain glob
must match at least one file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = ("CLAUDE.md", "AGENTS.md")
_TOP_DIRS = ("app/", "docs/", "outputs/", "snowflake/", "tests/", ".github/")
_FILE = re.compile(r"[\w./*-]+\.(?:md|py|sql|ini|toml|yml|yaml|txt|json)|[\w./*-]+/")
_PLACEHOLDER = re.compile(r"NNN|nnn|XX|<|>")


def _paths(doc: str) -> list[str]:
    text = (_ROOT / doc).read_text(encoding="utf-8")
    out = []
    for tok in re.findall(r"`([^`\n]+)`", text):
        if not _FILE.fullmatch(tok) or _PLACEHOLDER.search(tok):
            continue
        if tok.startswith(_TOP_DIRS) or ("/" not in tok and tok.endswith(".md")):
            out.append(tok)
    return out


def _resolves(path: str) -> bool:
    """A plain path must exist; a glob (the old `docs/handoff/*.md`) must match at least one file."""
    return any(_ROOT.glob(path)) if "*" in path else (_ROOT / path).exists()


@pytest.mark.parametrize("doc", _DOCS)
def test_every_backticked_repo_path_resolves(doc):
    paths = _paths(doc)
    assert len(paths) >= 5, f"{doc}: the path scan found almost nothing -- did the backtick style change?"
    missing = sorted({p for p in paths if not _resolves(p)})
    assert not missing, f"{doc} points at paths that do not exist: {missing}"
