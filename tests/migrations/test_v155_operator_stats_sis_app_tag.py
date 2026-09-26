"""V155 locks: the operator-stats collector ignores the app's own (SiS-stamped) statements.

Streamlit-in-Snowflake stamps every app statement with its own QUERY_TAG and overrides the app's, so
V147's 'OVERWATCH%' / '%OVERWATCH_APP%' filters never matched app traffic. V155 re-derives
SP_LOAD_QUERY_OPERATOR_STATS from V147 with ONE extra cursor predicate so it agrees with
ops_sql.query_optimization_triage again. Byte-locked to outputs/gen_v155.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from app import config
from app.data import ops_sql

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_NAME = "V155__operator_stats_sis_app_tag.sql"
_V155 = (_MIG / _NAME).read_text(encoding="utf-8")
_V147 = (_MIG / "V147__operator_stats_identity_grain.sql").read_text(encoding="utf-8")
_PROC_RE = re.compile(
    r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_QUERY_OPERATOR_STATS\(DAYS_BACK FLOAT\).*?\n\$\$;\n",
    re.S)
_INSERT = (
    "          -- V155: Streamlit-in-Snowflake stamps the app's own statements with this tag (it overrides the\n"
    "          -- app's), so match it too - parity with ops_sql.query_optimization_triage (common.not_app_self_sql).\n"
    "          AND NOT CONTAINS(COALESCE(qh.QUERY_TAG, ''), "
    "'\"StreamlitName\":\"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP\"')\n"
)


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v155_regenerates_byte_identical(tmp_path):
    out = tmp_path / _NAME
    env = {**os.environ, "V155_OUT": str(out)}
    subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v155.py")], check=True, env=env,
                   cwd=str(_ROOT), capture_output=True)
    assert out.read_text(encoding="utf-8") == _V155


def test_v155_is_one_guarded_proc_swap():
    assert "EXCEPTION (-20155, 'V155 requires V154 first" in _V155 and "IF (v < 154) THEN" in _V155
    assert "SELECT 155 AS VERSION" in _V155 and "WHERE VERSION = 155);" in _V155
    assert _V155.count("CREATE OR REPLACE PROCEDURE") == 1
    for banned in ("CREATE TABLE", "CREATE TASK", "ALTER TASK", "CALL DBA_MAINT_DB", "CREATE OR REPLACE VIEW"):
        assert banned not in _V155, banned
    # the only DELETE is the proc's own carried retention prune - no backfill/cleanup outside it
    (base,) = _PROC_RE.findall(_V147)
    assert _V155.count("DELETE FROM") == base.count("DELETE FROM") == 1
    assert "-- >>> derived:SP_LOAD_QUERY_OPERATOR_STATS (from V147;" in _V155


def test_v155_is_byte_identical_to_v147_except_the_one_predicate():
    (base,) = _PROC_RE.findall(_V147)
    (derived,) = _PROC_RE.findall(_V155)
    assert derived.count(_INSERT) == 1
    assert derived.replace(_INSERT, "", 1) == base


def test_the_fragment_is_the_apps_sis_tag():
    assert config.APP_SIS_QUERY_TAG_FRAGMENT in _INSERT


def test_collector_matches_triage_self_noise_filters():
    # the cursor documents 'query_optimization_triage's EXACT filter set'; every self-noise leg triage
    # applies must have a counterpart in the collector
    triage = ops_sql.query_optimization_triage(30)
    (derived,) = _PROC_RE.findall(_V155)
    for leg in ("NOT LIKE 'EXECUTE STREAMLIT%'", "NOT LIKE '%OVERWATCH_APP%'", "NOT LIKE 'OVERWATCH%'",
                config.APP_SIS_QUERY_TAG_FRAGMENT):
        assert leg in triage and leg in derived, leg


def test_validate_and_docs_track_v155():
    assert "V001..V159 applied" in _read("snowflake/validate.sql")
    assert "VERSION BETWEEN 1 AND 159) = 159" in _read("snowflake/validate.sql")
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _NAME in _read(rel), rel


def test_v155_in_expected_migrations():
    from app.ui.pages import admin
    assert 155 in admin._EXPECTED_MIGRATIONS
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in admin._EXPECTED_MIGRATIONS[155]
