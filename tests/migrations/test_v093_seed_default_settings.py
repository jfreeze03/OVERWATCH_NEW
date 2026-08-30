"""V093 locks: seed the DEFAULT_SETTINGS keys that no prior migration seeded.

17 Admin-editable keys (9 SCORE_PTS_* + 5 GOV_PTS_* + FORECAST_ENGINE +
EXPECTED_SPIKE_CALENDAR + DATA_TRANSFER_USD_PER_TB) had no SETTINGS row, so the
Admin table view was incomplete (the write path already UPSERTs, v4.343.0). V093
MERGE-seeds them with their code defaults, WHEN NOT MATCHED only. The recurrence
guard below asserts every editable DEFAULT_SETTINGS key is seeded by SOME migration,
so this drift cannot come back.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from app.config import DEFAULT_SETTINGS

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V093 = (_MIG / "V093__seed_default_settings.sql").read_text(encoding="utf-8")


def _editable_keys():
    return [k for k in DEFAULT_SETTINGS if not str(k).startswith("_")]


def test_v093_is_guarded_versioned_data_seed():
    assert "EXCEPTION (-20093" in _V093 and "IF (v < 92) THEN" in _V093
    assert "SELECT 93 AS VERSION" in _V093 and "WHERE VERSION = 93)" in _V093
    # data-seed only: a SETTINGS MERGE, never a schema/proc/view/task change
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" in _V093
    assert "CREATE TABLE" not in _V093 and "ALTER TABLE" not in _V093
    assert "CREATE OR REPLACE" not in _V093 and "CREATE TASK" not in _V093
    # WHEN NOT MATCHED only — never overwrite an operator's edited value
    assert "WHEN NOT MATCHED THEN INSERT" in _V093
    assert "WHEN MATCHED THEN UPDATE" not in _V093


def test_v093_seeds_each_unseeded_key_with_its_code_default():
    others = "".join(
        Path(p).read_text(encoding="utf-8")
        for p in sorted(glob.glob(str(_MIG / "V0*.sql")))
        if "V093" not in p
    )
    unseeded = [k for k in _editable_keys() if f"'{k}'" not in others]
    # V093 exists precisely to seed these; each with str(DEFAULT_SETTINGS[k])
    assert unseeded, "expected some previously-unseeded keys for V093 to cover"
    for key in unseeded:
        assert f"('{key}'" in _V093, f"V093 does not seed {key}"
        assert f"'{DEFAULT_SETTINGS[key]!s}'" in _V093, (
            f"V093 seeds {key} with a value other than its DEFAULT_SETTINGS default "
            f"{DEFAULT_SETTINGS[key]!r}")


def test_every_editable_default_setting_is_seeded_by_some_migration():
    # Recurrence guard: the union of ALL migrations must seed every editable
    # DEFAULT_SETTINGS key, so the Admin Settings table is never missing a row and a
    # future key added to config.py cannot silently go unseeded (the V093 defect).
    all_migrations = "".join(
        Path(p).read_text(encoding="utf-8")
        for p in sorted(glob.glob(str(_MIG / "V0*.sql")))
    )
    missing = [k for k in _editable_keys() if f"'{k}'" not in all_migrations]
    assert not missing, (
        "these DEFAULT_SETTINGS keys are not seeded by any migration — add them to "
        f"the latest seed migration: {missing}")


def test_v093_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V093):
        sqlglot.parse(statement, dialect="snowflake")
