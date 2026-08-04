"""Guardrail: OVERWATCH provisions NO warehouse other than the sanctioned
WH_ALFA_ADMIN, and the app runs on it.

Creating a new billable warehouse — or repointing the app's compute to one — is
an OWNER infrastructure decision, not an app/agent change (the same class as the
retired resource monitors). This test exists because a WH_OVERWATCH_APP creation
was once buried inside a 678-line migration and had to be reverted by hand; with
this guard, a repeat cannot pass CI, no matter how large the migration it hides in.

To sanction a NEW warehouse in future, the owner adds its exact name to
_SANCTIONED here in the same change — a deliberate, reviewable act.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# The ONLY warehouse the app/migrations may create or run on. Owner-controlled.
_SANCTIONED = {"WH_ALFA_ADMIN"}

# CREATE [OR REPLACE] [TRANSIENT] WAREHOUSE [IF NOT EXISTS] <name>
_CREATE_WH = re.compile(
    r"create\s+(?:or\s+replace\s+)?(?:transient\s+)?warehouse\s+"
    r'(?:if\s+not\s+exists\s+)?"?([A-Za-z0-9_$]+)"?',
    re.IGNORECASE,
)


def _sql_files() -> list[Path]:
    # DDL lives under snowflake/ (migrations + the generated rebuild bundle).
    return sorted((_ROOT / "snowflake").rglob("*.sql"))


def test_no_warehouse_created_other_than_sanctioned():
    offenders: list[str] = []
    for path in _sql_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _CREATE_WH.finditer(text):
            name = m.group(1).upper()
            if name not in _SANCTIONED:
                offenders.append(f"{path.relative_to(_ROOT)}: CREATE WAREHOUSE {name}")
    assert not offenders, (
        "Unauthorized warehouse creation — provisioning a warehouse other than "
        + "/".join(sorted(_SANCTIONED)) + " is an OWNER decision, not an app change. "
        "If this is intended, the owner adds the name to _SANCTIONED deliberately:\n  "
        + "\n  ".join(offenders)
    )


def test_app_warehouse_is_sanctioned():
    # The app's compute must stay on the sanctioned warehouse — repointing it
    # (config.APP_WAREHOUSE) is an owner infrastructure decision.
    from app.config import APP_WAREHOUSE

    assert APP_WAREHOUSE in _SANCTIONED, (
        f"APP_WAREHOUSE={APP_WAREHOUSE!r} is not sanctioned {sorted(_SANCTIONED)} — "
        "the app must run on the owner-approved warehouse."
    )
