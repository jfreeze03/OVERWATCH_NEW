"""V079 locks: the loader AI classification is aligned with the app canonical.

Re-derives four procs so their MATERIALIZED / alert AI split prices Cortex Code /
CoWork at the AI rate, matching the v4.158.0 app display
(app/data/common.ai_service_predicate). Three broaden the positive predicate
(SP_LOAD_PLATFORM_SCORE from V061, SP_ALERT_SCAN_DAILY from V066,
SP_REFRESH_EXEC_BOARD from V073); the fourth (SP_ALERT_SCAN from V067) rewrites
COST_SERVERLESS_CREEP's AI carve-out to the canonical not-AI form so CoCo is not
double-alerted as both AI and serverless. Rate/bucket-attribution only — total
credits unchanged."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V079 = (_MIG / "V079__ai_predicate_coco_historical_split.sql").read_text(encoding="utf-8")

_OLD = ("(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' "
        "OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')")
_NEW = ("(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' "
        "OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' "
        "OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%')")
_OLD_CARVE = ("              AND SERVICE_TYPE NOT IN "
              "('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER', 'AI_SERVICES')")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        text, re.S,
    ).group(0)


def test_v079_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v079.py")],
        env={**os.environ, "V079_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V079


def test_v079_is_four_guarded_proc_swaps_plus_refill():
    assert "EXCEPTION (-20079" in _V079 and "IF (v < 78) THEN" in _V079
    assert "SELECT 79 AS VERSION" in _V079 and "WHERE VERSION = 79)" in _V079
    assert _V079.count("CREATE OR REPLACE PROCEDURE") == 4
    assert "CREATE TABLE" not in _V079 and "CREATE TASK" not in _V079
    assert "CREATE WAREHOUSE" not in _V079 and "RESOURCE MONITOR" not in _V079
    # Re-fill the two MATERIALIZING loaders (idempotent); never fire either scanner.
    assert _V079.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(120);") == 1
    assert _V079.count("CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();") == 1
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" not in _V079
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in _V079


def test_v079_broadens_positive_predicate_and_leaves_none():
    assert _OLD not in _V079
    for name, n in (("SP_LOAD_PLATFORM_SCORE", 1),
                    ("SP_ALERT_SCAN_DAILY", 9),
                    ("SP_REFRESH_EXEC_BOARD", 2)):
        body = _proc(_V079, name)
        assert body.count(_NEW) == n, f"{name}: expected {n} broadened predicates"
        assert _OLD not in body
    # The broadened predicate is byte-equal to the app-side canonical helper.
    from app.data.common import ai_service_predicate
    assert ai_service_predicate() == _NEW


def test_v079_serverless_carveout_excludes_all_ai():
    # SP_ALERT_SCAN's COST_SERVERLESS_CREEP no longer excludes AI by the exact
    # name 'AI_SERVICES'; it now excludes ALL AI (incl. CoCo) via the canonical
    # not-AI predicate, so COST_AI_CREEP (which now counts CoCo) and
    # COST_SERVERLESS_CREEP stay mutually exclusive.
    from app.data.common import not_ai_service_predicate
    scan = _proc(_V079, "SP_ALERT_SCAN")
    assert "COST_SERVERLESS_CREEP" in scan
    assert _OLD_CARVE not in scan                     # the 3-name list is gone
    assert not_ai_service_predicate() in scan         # canonical not-AI exclusion present
    assert "%COCO%" in not_ai_service_predicate() and "%COWORK%" in not_ai_service_predicate()


def test_v079_leaves_proc_bodies_otherwise_at_their_base():
    # Reversing the change reproduces each proc byte-for-byte from its base —
    # the ONLY edit is the AI classification (predicate or carve-out).
    for base_file, name in (
        ("V061__ai_loader_alert_score_purge_fixes.sql", "SP_LOAD_PLATFORM_SCORE"),
        ("V066__alert_escalation_serverless_window_timeline_atomicity.sql", "SP_ALERT_SCAN_DAILY"),
        ("V073__calendar_triage_windows.sql", "SP_REFRESH_EXEC_BOARD"),
    ):
        base_proc = _proc((_MIG / base_file).read_text(encoding="utf-8"), name)
        assert _proc(_V079, name).replace(_NEW, _OLD) == base_proc, f"{name} diverged beyond the predicate"
    # SP_ALERT_SCAN: reversing the carve-out reproduces the V067 base.
    from app.data.common import not_ai_service_predicate
    new_carve = ("              AND SERVICE_TYPE NOT IN "
                 "('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')\n"
                 "              AND " + not_ai_service_predicate())
    base_scan = _proc((_MIG / "V067__alert_attribution_onset_supersede_objectcost.sql")
                      .read_text(encoding="utf-8"), "SP_ALERT_SCAN")
    assert _proc(_V079, "SP_ALERT_SCAN").replace(new_carve, _OLD_CARVE) == base_scan


def test_v079_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V079):
        sqlglot.parse(statement, dialect="snowflake")
