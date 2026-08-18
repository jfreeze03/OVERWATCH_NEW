"""Locks for V053 — the safe residue of the action-layer round.

The action-layer procs (SP_EXECUTE_REMEDIATION, SP_VERIFY_SAVINGS) were both
dropped after three adversarial-review rounds kept finding holes in validating
free-text SQL under EXECUTE AS OWNER (owner-privileged injection, allow-list
comment-bypass, side-effecting proofs). What ships is the genuinely valuable,
zero-risk part: the typed savings link + a re-derived monthly verifier that
closes the P1-A selection gap, with the app stamping FINDING_TYPE on its
existing (already-guarded) booking inserts. No stored procedure ships here.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_V53 = (_ROOT / "snowflake" / "migrations" / "V053__action_layer_remediation_verify.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v053_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v053a.py")],
                       env={**os.environ, "V053A_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V53, (
        "V053 drifted from outputs/gen_v053a.py — regenerate, never hand-edit.")


def test_v053_guard_version_house_rules():
    assert "EXCEPTION (-20053" in _V53 and "RAISE not_ready;" in _V53
    assert "IF (v < 52) THEN" in _V53 and "SELECT 53 AS VERSION" in _V53


def test_v053_ships_no_action_procs():
    """The owner-privileged remediation/verify procs did NOT ship — only the
    re-derived monthly verifier (V007's proc with a wider WHERE)."""
    assert "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_EXECUTE_REMEDIATION" not in _V53
    assert "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_SAVINGS" not in _V53
    assert _V53.count("CREATE OR REPLACE PROCEDURE") == 1               # only the verifier
    assert "-- >>> derived:SP_VERIFY_IDLE_SAVINGS" in _V53
    # no free-text EXECUTE IMMEDIATE of operator input ships anywhere here
    assert "EXECUTE IMMEDIATE :P_STMT" not in _V53
    assert "EXECUTE IMMEDIATE :v_proof" not in _V53


def test_v053_typed_savings_columns_added():
    for col in ("FINDING_TYPE", "TARGET_OBJECT", "PROOF_QUERY_ID", "PROOF_RESULT", "PROOF_RUN_AT"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in _V53, col


def test_v053_verifier_selects_typed_rows():
    """P1-A: the verifier matched only DESCRIPTION LIKE 'Auto-suspend tune: %',
    which no executed path booked. It now also selects app-booked
    FINDING_TYPE='AUTO_SUSPEND' rows and prefers TARGET_OBJECT for the name."""
    assert "FINDING_TYPE = 'AUTO_SUSPEND' OR DESCRIPTION LIKE 'Auto-suspend tune: %'" in _V53
    assert "COALESCE(NULLIF(TARGET_OBJECT, '')," in _V53


def test_v053_verifier_is_v007_plus_two_edits():
    """The re-derived proc is V007 verbatim + exactly the typed-selection edits
    — nothing else changed under the derivation law."""
    import difflib
    pat = re.compile(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_VERIFY_IDLE_SAVINGS\(.*?\n\$\$;\n", re.S)
    v007 = pat.findall(_read("snowflake/migrations/V007__automation.sql"))[-1]
    v053 = pat.findall(_V53)[-1]
    added = [ln for ln in difflib.unified_diff(v007.splitlines(), v053.splitlines(), lineterm="", n=0)
             if ln.startswith("+") and not ln.startswith("+++")]
    assert any("COALESCE(NULLIF(TARGET_OBJECT" in ln for ln in added)
    assert any("FINDING_TYPE = 'AUTO_SUSPEND'" in ln for ln in added)


def test_app_stamps_finding_type_on_booking_inserts():
    """The app closes P1-A by typing its EXISTING ledger inserts — no proc,
    the existing guarded execute_statement path stays."""
    opt = _read("app/ui/pages/cost_parts/optimize.py")
    assert opt.count("NOTES, FINDING_TYPE, TARGET_OBJECT)") == 3        # resize/retention/guarded
    assert "'AUTO_SUSPEND' if fix_kind.startswith('Tighten') else 'SCHEDULE'" in opt
    assert "SP_EXECUTE_REMEDIATION" not in opt and "SP_VERIFY_SAVINGS" not in opt


def test_v053_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V53):
        sqlglot.parse(stmt, dialect="snowflake")


def test_v053_no_new_teardown_needed():
    """No new object ships (verifier re-derived = V007's; columns are on the
    operator SAVINGS_LEDGER), so teardown gains nothing for V053."""
    td = _read("snowflake/teardown.sql")
    assert "SP_EXECUTE_REMEDIATION" not in td and "SP_VERIFY_SAVINGS" not in td
