"""V125: MFA-gap posture on the app-wide COALESCE active-user canonical.

The mart loader's MFA_GAP_USERS arm was the lone hold-out still filtering active
users with a bare ``U.DISABLED = FALSE``. Every live reader uses
``COALESCE(U.DISABLED, FALSE) = FALSE`` (locked by test_bughunt_round18), so a
NULL-DISABLED password-login user without MFA was counted live but dropped by the
warm mart. V125 re-derives SP_LOAD_MARTS_V27 from V113 with the arm on COALESCE and
NOTHING else changed. These locks pin the mart side and the byte-fidelity of the
re-derivation, so a future SP_LOAD_MARTS_V27 change cannot silently reopen the gap.
"""

from __future__ import annotations

import difflib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DIR = _ROOT / "snowflake" / "migrations"
_V125 = (_MIG_DIR / "V125__mfa_gap_active_user_coalesce.sql").read_text(encoding="utf-8")
_V113 = (_MIG_DIR / "V113__incident_timeline_task_fail_completed_time.sql").read_text(encoding="utf-8")

_CANON = "COALESCE(U.DISABLED, FALSE) = FALSE"
_BARE = "AND U.DISABLED = FALSE"


def _proc_block(sql: str) -> str:
    """The CREATE OR REPLACE PROCEDURE ... $$; body for SP_LOAD_MARTS_V27."""
    start = sql.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27")
    end = sql.index("\n$$;", start) + len("\n$$;")
    return sql[start:end]


def test_v125_guarded_versioned_and_proc_only() -> None:
    assert "EXCEPTION (-20125" in _V125 and "RAISE not_ready;" in _V125
    assert "IF (v < 124) THEN" in _V125
    assert "SELECT 125 AS VERSION" in _V125
    assert "WHERE VERSION = 125" in _V125
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in _V125
    # forward-healing like its V113 parent: no in-migration CALL, no new object /
    # schema change (the nightly SP_LOAD_MARTS_V27 task re-stamps the mart).
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" not in _V125
    assert "CREATE TABLE" not in _V125 and "ALTER TABLE" not in _V125


def test_v125_mfa_gap_arm_uses_the_coalesce_canonical() -> None:
    body = _proc_block(_V125)
    assert "'MFA_GAP_USERS'" in body
    # the MFA-gap arm now filters active users the same way every live reader does
    assert f"WHERE U.DELETED_ON IS NULL AND {_CANON}" in body
    # and the bare form is gone from the whole migration
    assert _BARE not in _V125


def test_v125_is_a_one_line_re_derivation_of_v113() -> None:
    """Byte-fidelity: the V125 proc body differs from V113's by EXACTLY the MFA
    line — nothing else in the 851-line loader moved. This permanently encodes the
    generation-time verification, so any future edit that drifts the proc fails CI."""
    old, new = _proc_block(_V113), _proc_block(_V125)
    diff = [d for d in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if d and d[0] in "+-" and not d.startswith(("+++", "---"))]
    removed = [d for d in diff if d.startswith("-")]
    added = [d for d in diff if d.startswith("+")]
    assert len(removed) == 1 and len(added) == 1, f"expected 1 changed line, got {diff}"
    assert _BARE in removed[0] and _CANON in added[0]


def test_v125_is_the_latest_full_loader_definition() -> None:
    """V125 must be the newest migration carrying a full SP_LOAD_MARTS_V27 body, so
    the next re-derivation starts from it (not the now-superseded V113)."""
    defs = sorted(p for p in _MIG_DIR.glob("V[0-9]*.sql")
                  if "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27"
                  in p.read_text(encoding="utf-8"))
    assert defs[-1].name == "V125__mfa_gap_active_user_coalesce.sql"


def test_v125_floor_tracks_the_tip() -> None:
    v = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V125 applied" in v
    assert "BETWEEN 1 AND 125) = 125" in v


def test_v125_is_tracked_in_deploy_and_admin_surfaces() -> None:
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V125__mfa_gap_active_user_coalesce.sql" in (_ROOT / rel).read_text(encoding="utf-8")
    assert "125:" in (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
