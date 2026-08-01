"""No live credential may be committed to this repo.

History: `snowflake/webhook_delivery.sql` carried a REAL Power Automate trigger URL —
including its `sig=` bearer token — from 2026-07-08 to 2026-07-31, tracked in git and
pushed to GitHub. Anyone with read access to the repo could post into the Teams channel.
The file's own header had always said the secret "cannot ship in git"; nothing enforced it.

This is that enforcement. It is deliberately narrow — it looks for the shapes of real
secrets, not for the word "secret" — so it stays quiet on templates and placeholders and
screams on the real thing.

If this fails: do NOT just edit the file. The value is already in git history, so the only
real remedy is to ROTATE the credential at the provider, then replace it here with a
placeholder.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# TODO(ci): this test scans the CURRENT working tree only. A credential that was
# committed and later removed still lives in git HISTORY (exactly how the leaked
# Teams URL escaped). Add a dedicated CI step that scans history — e.g.
# `gitleaks detect --source . --log-opts=--all` (or trufflehog) as its own job.
# Do NOT reimplement a git-history scan inside pytest; keep this a working-tree check.

# Security-relevant text suffixes. git ls-files (below) gives us the WHOLE tracked
# tree; this set just decides which of those files are worth opening and scanning.
_SCAN_SUFFIXES = {
    ".sql", ".py", ".md", ".toml", ".yml", ".yaml", ".json",
    ".env", ".pem", ".key", ".cfg", ".ini", ".sh", ".txt", ".cnf", ".conf",
}

# Basenames that are security-relevant even without a scanned suffix
# (dotfiles like `.env`, extensionless key material).
_SCAN_NAMES = {".env", ".netrc", "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa"}

# Real-credential shapes. Each needs a value that a placeholder would not satisfy.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Power Automate / Logic Apps trigger signature: sig=<20+ url-safe base64 chars>
    ("power-automate sig token", re.compile(r"[?&]sig=[A-Za-z0-9_\-]{20,}")),
    # Slack incoming webhook: /services/T.../B.../<24+ chars>
    ("slack webhook url", re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{16,}")),
    # Snowflake/AWS-ish long-lived keys pasted into SQL or config
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    # PAT-style tokens
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
)

# Placeholders that intentionally look credential-shaped. Keep this list SHORT and exact —
# every entry is a hole in the check.
_ALLOWED_SUBSTRINGS = (
    "<REDACTED-PASTE-IN-SNOWSIGHT>",
    "T000/B000/XXXX",          # the documented Slack recipe placeholder
    "<slack-webhook-url>",
)


def _files():
    """Every security-relevant, git-tracked file in the repo (Wave 3 #38).

    Was: rglob over four hand-picked subdirs (snowflake/app/outputs/docs), which
    left root files, `.github/workflows/*.yml`, and `tests/` entirely unscanned.
    Enumerating the whole tracked tree via `git ls-files` closes those blind spots
    and mirrors the repo exactly — it never opens build junk or untracked local
    files. Falls back to a whole-repo walk when git is unavailable (source tarball).
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=_ROOT, capture_output=True, text=True, check=True,
        ).stdout
        rels = [r for r in out.split("\0") if r]
    except (OSError, subprocess.SubprocessError):
        rels = [p.relative_to(_ROOT).as_posix()
                for p in _ROOT.rglob("*") if p.is_file()]
    for rel in rels:
        p = _ROOT / rel
        if "__pycache__" in p.parts:
            continue
        if (p.suffix.lower() in _SCAN_SUFFIXES or p.name in _SCAN_NAMES) and p.is_file():
            yield p


def test_no_live_credentials_committed():
    hits: list[str] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in _PATTERNS:
            for m in pattern.finditer(text):
                snippet = m.group(0)
                if any(a in snippet or a in text[max(0, m.start() - 80):m.end() + 80]
                       for a in _ALLOWED_SUBSTRINGS):
                    continue
                line = text[:m.start()].count("\n") + 1
                # Report the SHAPE and location, never the value itself.
                hits.append(f"{path.relative_to(_ROOT).as_posix()}:{line} — {label} "
                            f"({len(snippet)} chars)")
    assert not hits, (
        "Live credential(s) found in tracked files:\n  " + "\n  ".join(hits)
        + "\n\nROTATE the credential at the provider first — it is already in git history, "
          "so deleting the line does not undo the exposure — then replace it here with a "
          "placeholder. See snowflake/webhook_delivery.sql for the template form."
    )


def test_webhook_delivery_file_stays_a_template():
    """The specific file that leaked: it must stay placeholder-only."""
    p = _ROOT / "snowflake" / "webhook_delivery.sql"
    text = p.read_text(encoding="utf-8")
    assert "<REDACTED-PASTE-IN-SNOWSIGHT>" in text, "the Teams secret must stay a placeholder"
    assert "NEVER PASTE THE REAL URL INTO THIS FILE" in text, "the warning must stay in place"
    # the exact token that leaked must never reappear
    assert "jVLNX37r" not in text
