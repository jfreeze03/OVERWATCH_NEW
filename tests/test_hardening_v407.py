"""Hardening locks for the full-app adversarial sweep (v4.407.0).

Four confirmed findings, each pinned so it cannot silently regress:
- run()'s except path must survive a driver exception with a raising __str__ (the
  'never raises' contract) — _classify_error and format_snowflake_error are guarded.
- Operations Queries tile labels the window actually SERVED (live fallback clamps to 90d).
- per-user SENSITIVE_PRIVILEGES is de-duplicated by effective role, not summed per path.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core.errors import format_snowflake_error
from app.core.query import _classify_error
from app.logic.security import sensitive_privileges_by_user

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


class _RaisingStr:
    """Mimics a driver exception whose __str__ lazily formats a missing attribute."""

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        raise RuntimeError("cannot render this exception")


# ---------------------------------------------------------------------------
# #1 run()'s except path must not re-raise on a hostile __str__
# ---------------------------------------------------------------------------
def test_classify_error_survives_a_raising_str():
    # run() calls _classify_error FIRST in its except block (before the guarded
    # record_error), so this conversion must not re-raise or run() breaks 'never raises'.
    assert _classify_error(_RaisingStr()) == "other"        # conservative kind, no raise


def test_format_snowflake_error_survives_a_raising_str():
    out = format_snowflake_error(_RaisingStr())
    assert "_RaisingStr" in out and "unavailable" in out    # typed placeholder, no raise


def test_classify_error_still_classifies_normal_messages():
    # the guard must not change classification of ordinary errors
    assert _classify_error(Exception("Object does not exist or not authorized")) == "absent"
    assert _classify_error(Exception("Statement reached its statement or warehouse timeout")) == "timeout"
    assert _classify_error(Exception("invalid identifier 'TOKENS'")) == "missing_column"


# ---------------------------------------------------------------------------
# #3 Operations Queries tile labels the SERVED window, not the requested one
# ---------------------------------------------------------------------------
def test_operations_queries_tile_labels_served_window_not_requested():
    src = _src("app/ui/pages/operations.py")
    # the live fallback clamps to 90d; the label must use the served window
    assert "_served_days = days if used_mart else min(days, MAX_LIVE_WINDOW_DAYS)" in src
    assert 'f"Queries ({_served_days}d)"' in src
    # the raw-days label (the bug) must be gone
    assert 'f"Queries ({days}d)"' not in src
    assert "from app.config import MAX_LIVE_WINDOW_DAYS" in src


# ---------------------------------------------------------------------------
# #4 SENSITIVE_PRIVILEGES de-duplicated by effective role, not summed per path
# ---------------------------------------------------------------------------
def test_sensitive_privileges_dedup_shared_inherited_role():
    # user U reaches ADMIN_UTIL (1 sensitive priv) via TWO direct roles -> two rows, each
    # carrying the per-role SENSITIVE_PRIVILEGES=1. The old sum() reported 2; the dedup
    # by (USER_NAME, EFFECTIVE_ROLE) must report the true reachable count of 1.
    frame = pd.DataFrame({
        "USER_NAME": ["U", "U"],
        "DIRECT_ROLE": ["R1", "R2"],
        "EFFECTIVE_ROLE": ["ADMIN_UTIL", "ADMIN_UTIL"],
        "ACCESS_PATH": ["R1 -> ADMIN_UTIL", "R2 -> ADMIN_UTIL"],
        "SENSITIVE_PRIVILEGES": [1, 1],
    })
    out = sensitive_privileges_by_user(frame)
    assert out.loc[out["USER_NAME"] == "U", "SENSITIVE_PRIVILEGES"].iloc[0] == 1


def test_sensitive_privileges_sums_across_distinct_roles():
    # two DISTINCT effective roles each with sensitive privs DO sum (2 + 3 = 5) — the fix
    # collapses duplicate paths, it does not collapse genuinely different roles.
    frame = pd.DataFrame({
        "USER_NAME": ["U", "U", "U"],
        "DIRECT_ROLE": ["R1", "R1", "R2"],
        "EFFECTIVE_ROLE": ["ROLE_A", "ROLE_A", "ROLE_B"],  # ROLE_A appears twice (one path dup)
        "ACCESS_PATH": ["R1 -> ROLE_A", "R1 -> X -> ROLE_A", "R2 -> ROLE_B"],
        "SENSITIVE_PRIVILEGES": [2, 2, 3],
    })
    out = sensitive_privileges_by_user(frame)
    assert out.loc[out["USER_NAME"] == "U", "SENSITIVE_PRIVILEGES"].iloc[0] == 5


def test_sensitive_privileges_safe_on_empty_and_missing_columns():
    empty = sensitive_privileges_by_user(pd.DataFrame())
    assert list(empty.columns) == ["USER_NAME", "SENSITIVE_PRIVILEGES"] and empty.empty
    # returns int dtype so it renders as a whole count
    frame = pd.DataFrame({"USER_NAME": ["A"], "EFFECTIVE_ROLE": ["R"], "SENSITIVE_PRIVILEGES": [4]})
    out = sensitive_privileges_by_user(frame)
    assert str(out["SENSITIVE_PRIVILEGES"].dtype) == "int64"
