"""V044 locks — UNKNOWN classification (adjudication #18, owner: "do 18").

Evidence-based company on BOTH sides; COMPANY_SCOPE rows are the explicit
lever; the residual is UNKNOWN and surfaces on Cost -> Chargeback.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V044__unknown_classification.sql").read_text(encoding="utf-8")


def test_v044_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v044.py")],
                       env={**os.environ, "V044_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _MIG, (
        "V044 drifted from its forward-generation — edit outputs/gen_v044.py, "
        "regenerate, never hand-edit the migration.")


def test_v044_udfs_are_evidence_based_with_unknown_residual():
    wh = _MIG.split("-- >>> derived:COMPANY_FOR_WAREHOUSE", 1)[1].split("-- >>> derived:", 1)[0]
    assert "LIKE 'WH!_ALFA!_%' ESCAPE '!'" in wh and "'UNKNOWN'" in wh
    db = _MIG.split("-- >>> derived:COMPANY_FOR_DATABASE", 1)[1].split("-- >>> derived:", 1)[0]
    assert "SCOPE_TYPE = 'DATABASE'" in db            # mapping lever, database grain
    assert "LIKE 'ALFA%'" in db and "'UNKNOWN'" in db
    usr = _MIG.split("-- >>> derived:COMPANY_FOR_USER", 1)[1].split("-- >>> derived:", 1)[0]
    assert "ILIKE '%ALFA%'" in usr
    assert "'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'" in usr   # DBA roles are ALFA staff
    assert "'ALFA', 'UNKNOWN'))" in usr
    assert "USER_OVERRIDE" in usr                      # KEBARR1-style overrides still win


def test_v044_board_scope_and_seed_and_history_note():
    board = _MIG.split("-- >>> derived:SP_REFRESH_EXEC_BOARD", 1)[1].split("-- >>> seeds", 1)[0]
    assert "UNION ALL SELECT 'UNKNOWN'" in board       # the pill is mart-served
    seeds = _MIG.split("-- >>> seeds", 1)[1]
    assert "'DATABASE' AS SCOPE_TYPE, 'DBA_MAINT_DB' AS PATTERN, 'ALFA' AS COMPANY" in seeds
    assert "HISTORY NOTE" in _MIG                      # honest re-stamp semantics


def test_app_mirrors_and_pill_and_worklist():
    from app import companies as co
    assert co.COMPANIES == ("ALFA", "Trexis", "UNKNOWN", "ALL")
    assert co.classify_warehouse("COMPUTE_WH") == "UNKNOWN"
    assert co.classify_database("RANDOM_DB") == "UNKNOWN"
    assert co.classify_database("DBA_MAINT_DB") == "ALFA"     # app infra, seeded
    assert "UNKNOWN" in co.user_clause("UNKNOWN")
    assert "NOT LIKE" in co.warehouse_clause("UNKNOWN")
    from app.data import mart_sql
    sql = mart_sql.unmapped_entities(7)
    assert "COMPANY = 'UNKNOWN'" in sql and "ACCOUNT_USAGE" not in sql   # mart-only worklist
    cost = (_ROOT / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    assert "Unmapped entities" in cost
    assert "COMPANY_SCOPE (SCOPE_TYPE, PATTERN, COMPANY)" in cost        # the fix, printed
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "mart_sql.unmapped_entities" in canary


def test_validate_flipped_to_the_new_law():
    v = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "Unknown user classifies UNKNOWN (V044: evidence-based)" in v
    assert "= 'UNKNOWN', 'OK', 'FAIL')" in v
    assert "KEBARR1 must classify as ALFA" in v        # override law unchanged
