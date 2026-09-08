"""Locks for V132 — DQ_BREACH data-quality alert (Codex R24).

SP_ANOMALY_SWEEP gains a DQ_BREACH arm that books the Operations data-quality panel's row-volume
robust-z findings as alerts. The arm must use the SAME scoring gates as logic/dq.row_volume_anomalies
so the panel and the alert can never disagree about what counts as a data-quality breach.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.logic.dq import row_volume_anomalies

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V132__dq_breach_alert.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v132_guarded_and_ordered():
    assert "EXCEPTION (-20132" in _MIG and "IF (v < 131) THEN" in _MIG
    assert "SELECT 132 AS VERSION" in _MIG and "WHERE VERSION = 132)" in _MIG


def test_v132_seeds_dq_breach_rule():
    assert "('DQ_BREACH', 'PIPELINE'" in _MIG
    assert "'MEDIUM', 3.5, 24)" in _MIG   # threshold 3.5 == dq.py's z_threshold default
    assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in _MIG
    assert "WHEN MATCHED THEN UPDATE" not in _MIG   # never clobber an operator's edited rule


def test_v132_arm_reproduces_the_dq_panel_scoring():
    # the DB arm must reuse the SAME robust-z gates as logic/dq.row_volume_anomalies (panel <-> alert)
    assert "-- DQ_BREACH (R24)" in _MIG and "RULE_ID = 'DQ_BREACH'" in _MIG
    assert "MAD_RAW * 1.4826" in _MIG               # MAD -> sd scale (dq._MAD_SCALE)
    assert "0.15 * b.MED" in _MIG                   # dispersion floor (dq._SCALE_FLOOR_FRAC)
    assert "N_LOADS >= 11" in _MIG                  # min_baseline_loads(10) baseline + the latest load
    assert "b.MED >= 100" in _MIG                   # min_median_rows
    assert "ABS(s.RAW_Z) >= c.THRESHOLD_NUM" in _MIG   # flag both directions (spike OR drop)
    assert "WHERE RN > 1 GROUP BY" in _MIG          # baseline EXCLUDES the latest load
    # the migration's constants must track dq.py's defaults — if a default changes, this fails
    sig = inspect.signature(row_volume_anomalies)
    assert sig.parameters["z_threshold"].default == 3.5
    assert sig.parameters["min_baseline_loads"].default == 10
    assert sig.parameters["min_median_rows"].default == 100.0
    # CRITICAL (verify HIGH finding): the arm's load window MUST equal the panel's
    # product_row_volume(28) default, or the alert and the panel score different baselines over
    # different windows and disagree on the same table.
    from app.data import dq_sql
    assert inspect.signature(dq_sql.product_row_volume).parameters["days"].default == 28
    assert "DATEADD('day', -28, CURRENT_DATE())" in _MIG
    assert "-60" not in _MIG


def test_v132_rederives_proc_nothing_dropped():
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "COST_ANOMALY_SWEEP" in _MIG and "PIPE_VOLUME_DROP" in _MIG
    assert "RETURN 'anomaly sweep v3 complete'" in _MIG
    # the arm is exception-guarded like the sibling ACCOUNT_USAGE arms
    assert "'DQ_BREACH check skipped'" in _MIG
    assert "CREATE TABLE " not in _MIG and "ALTER TABLE " not in _MIG
    # re-runs the sweep once so current anomalies populate on apply
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP();" in _MIG


def test_validate_and_docs_track_v132():
    val = _read("snowflake/validate.sql")
    assert "V001..V133 applied" in val and "VERSION BETWEEN 1 AND 133) = 133" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V132__dq_breach_alert.sql" in _read(rel)


def test_v132_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 132 in _EXPECTED_MIGRATIONS


def test_v132_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
