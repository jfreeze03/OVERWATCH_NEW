"""Locks for V142 — A4: collapse the posture arm's CREDENTIALS + GRANTS_TO_USERS double-scans.

SP_LOAD_MARTS_V27's [7] security-posture arm scanned CREDENTIALS twice (EXPIRING_CRED_10D +
EXPIRED_CRED) and GRANTS_TO_USERS twice (GRANT_CHANGES_24H + BREAKGLASS_GRANTS_30D). V142 re-derives
the proc so each source is scanned ONCE with COUNT_IF conditional aggregation, UNPIVOTed back to the
same (DAY, METRIC, COMPANY, VALUE) rows. Output-equivalent by construction (COUNT_IF(cond) ==
COUNT(*) WHERE cond; the GRANTS one-scan WHERE is a superset of both metrics' rows). Everything
outside the posture arm is byte-identical to V127.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V142 = (_MIG / "V142__posture_arm_single_scan.sql").read_text(encoding="utf-8")
_V127 = (_MIG / "V127__wh_eff_idle_credits_actual_hours.sql").read_text(encoding="utf-8")

_SIG = "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)"
_HEAD = "-- [7] security posture"
_TAIL = "loaded := loaded || 'posture ';"


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{_SIG}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _parts(proc: str):
    head, _, rest = proc.partition(_HEAD)
    arm, _, tail = rest.partition(_TAIL)
    return head, arm, tail


def test_v142_guarded_and_ordered():
    assert "EXCEPTION (-20142" in _V142 and "IF (v < 141) THEN" in _V142
    assert "SELECT 142 AS VERSION" in _V142 and "WHERE VERSION = 142)" in _V142


def test_v142_only_the_posture_arm_changed():
    """Strong guard: the proc is byte-identical to V127 everywhere except the [7]
    security-posture arm, so nothing else in the ~900-line mart loader shifted."""
    h127, a127, t127 = _parts(_proc(_V127))
    h142, a142, t142 = _parts(_proc(_V142))
    assert h127 and t127 and h142 and t142           # partitions actually split
    assert h127 == h142, "code before the posture arm drifted"
    assert t127 == t142, "code after the posture arm drifted"
    assert a127 != a142, "the posture arm should have changed"


def test_v142_each_source_scanned_once_now():
    p = _proc(_V142)
    assert p.count("FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS") == 1      # was 2
    assert p.count("FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS") == 1  # was 2
    # V127 really did scan them twice (guards against a stale baseline)
    v = _proc(_V127)
    assert v.count("FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS") == 2
    assert v.count("FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS") == 2


def test_v142_all_four_metrics_kept_with_exact_predicates():
    # every metric the MERGE produced still exists, via UNPIVOT column names
    for metric in ("EXPIRING_CRED_10D", "EXPIRED_CRED", "GRANT_CHANGES_24H", "BREAKGLASS_GRANTS_30D"):
        assert f'"{metric}"' in _V142
    assert _V142.count("UNPIVOT (VALUE FOR METRIC IN") == 2
    # the COUNT_IF predicates are byte-for-byte the original WHERE predicates ->
    # counts cannot change, only the number of scans
    assert "EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())" in _V142
    assert "COUNT_IF(EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP())" in _V142
    assert "ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')" in _V142
    # the GRANTS one-scan WHERE is a superset of the rows either metric needs
    assert ("WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())\n"
            "                       OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())") in _V142


def test_v142_leaves_the_anomaly_sweep_double_scan_alone():
    # A4 deliberately did NOT touch SP_ANOMALY_SWEEP (different windows/grains + per-arm isolation);
    # V142 re-derives only SP_LOAD_MARTS_V27, which has its own TABLE_DML volume arm elsewhere.
    # V142 re-derives ONLY SP_LOAD_MARTS_V27 (the comment/description mention SP_ANOMALY_SWEEP just to
    # record that it was deliberately left alone).
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP" not in _V142
    assert _V142.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27") == 1


def test_validate_and_docs_track_v142():
    val = _read("snowflake/validate.sql")
    assert "V001..V142 applied" in val and "VERSION BETWEEN 1 AND 142) = 142" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V142__posture_arm_single_scan.sql" in _read(rel)


def test_v142_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 142 in _EXPECTED_MIGRATIONS


def test_v142_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V142):
        sqlglot.parse(statement, dialect="snowflake")
