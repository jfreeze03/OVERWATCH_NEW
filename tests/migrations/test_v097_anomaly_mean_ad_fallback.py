"""V097 locks: SP_ANOMALY_SWEEP mean-absolute-deviation fallback when MAD=0.

The COST_ANOMALY_SWEEP arm hard-filtered `WHERE l.MAD > 0`, so a majority-idle / intermittent
series whose median-absolute-deviation collapses to 0 silently dropped its spike. V097
re-derives the proc with a meanad sibling CTE and a MAD-first / mean-AD-second robust-z
denominator (matching the app twin app/logic/anomaly.py robust_zscores constants + gate order),
replacing the hard MAD>0 filter with a chosen-denominator>0 guard. Byte-locked to
outputs/gen_v097.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V097 = (_MIG / "V097__anomaly_mean_ad_fallback.sql").read_text(encoding="utf-8")
_V076 = (_MIG / "V076__anomaly_materiality_gate.sql").read_text(encoding="utf-8")


def test_v097_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v097.py")],
        env={**os.environ, "V097_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V097, (
        "V097 drifted from its forward-generation — edit outputs/gen_v097.py, "
        "not the .sql, then regenerate."
    )


def test_v097_is_one_guarded_proc_redefinition():
    assert "EXCEPTION (-20097" in _V097 and "IF (v < 96) THEN" in _V097
    assert "SELECT 97 AS VERSION" in _V097 and "WHERE VERSION = 97)" in _V097
    assert _V097.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP" in _V097
    assert "CREATE TABLE" not in _V097 and "ALTER TABLE" not in _V097
    assert "CREATE OR REPLACE VIEW" not in _V097 and "CREATE TASK" not in _V097


def test_v097_has_mean_ad_fallback_and_drops_the_hard_mad_filter():
    # the mean-AD sibling CTE (== abs_dev.mean() in the app twin)
    assert "meanad AS (" in _V097
    assert "AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD" in _V097
    assert "JOIN meanad ma ON ma.SERIES = s.SERIES" in _V097
    # MAD-first / mean-AD-second denominator with the matching constants
    assert "IFF(m.MAD > 0, 0.6745, 0.7979)" in _V097
    assert "NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)" in _V097
    # the hard MAD>0 drop is replaced by a chosen-denominator>0 guard (check the proc body,
    # not the header comment which quotes the old predicate)
    assert "WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr" in _V097
    body = _V097.split("CREATE OR REPLACE PROCEDURE", 1)[1]
    assert "WHERE l.MAD > 0" not in body
    # V076 (base) still hard-filters MAD>0, confirming V097 supersedes it
    assert "WHERE l.MAD > 0 AND l.ROBUST_Z >= :zthr" in _V076


def test_v097_constants_match_the_app_twin():
    from app.logic.anomaly import _MAD_K, _MEANAD_K
    assert _MAD_K == 0.6745 and _MEANAD_K == 0.7979
    assert str(_MAD_K) in _V097 and str(_MEANAD_K) in _V097


def test_v097_materiality_gates_are_unchanged():
    for gate in (
        "AND l.ACTIVE_DAYS >= 10",
        "(l.SIGNED_Z > 0 AND l.CREDITS * :credit_price >= 50)",
        "OR (l.SIGNED_Z < 0 AND l.MED * :credit_price >= 50)",
    ):
        assert gate in _V097, gate


def test_v097_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V097):
        sqlglot.parse(statement, dialect="snowflake")
