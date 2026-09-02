"""Locks for V121 — seed COCO_DAILY_CAP_CREDITS (round-8 SC-1).

The per-user daily Cortex Code allowance the token-economics efficiency review measures
against was read (ai_chargeback) but never in DEFAULT_SETTINGS / seeded / editable, so it
was pinned to the 15.0 fallback with no configuration path. It is now in DEFAULT_SETTINGS +
_SETTING_EDITORS; V121 seeds its default row (data-seed, WHEN NOT MATCHED)."""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_SETTINGS

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V121__seed_coco_daily_cap.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v121_guard_and_data_seed_shape():
    assert "EXCEPTION (-20121" in _MIG and "IF (v < 120) THEN" in _MIG
    assert "SELECT 121 AS VERSION" in _MIG and "WHERE VERSION = 121)" in _MIG
    # data-seed ONLY — a SETTINGS MERGE, never a schema/proc/view/task change
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT" in _MIG and "WHEN MATCHED THEN UPDATE" not in _MIG
    assert "CREATE TABLE" not in _MIG and "CREATE OR REPLACE" not in _MIG and "CREATE TASK" not in _MIG


def test_v121_seeds_coco_cap_with_its_code_default():
    assert "COCO_DAILY_CAP_CREDITS" in DEFAULT_SETTINGS          # now a real editable key
    assert f"('COCO_DAILY_CAP_CREDITS', '{DEFAULT_SETTINGS['COCO_DAILY_CAP_CREDITS']!s}')" in _MIG


def test_coco_cap_is_wired_across_all_three_config_sites():
    # reader, DEFAULT_SETTINGS, and the Admin editor must all know the key now (SC-1).
    assert 'settings.get("COCO_DAILY_CAP_CREDITS")' in _read("app/ui/pages/cost_parts/ai_chargeback.py")
    assert '"COCO_DAILY_CAP_CREDITS": 15.0' in _read("app/config.py")
    assert '"COCO_DAILY_CAP_CREDITS": (_NUM,' in _read("app/ui/pages/admin.py")


def test_validate_floor_and_docs_track_v121():
    val = _read("snowflake/validate.sql")
    assert "V001..V122 applied" in val and "VERSION BETWEEN 1 AND 122) = 122" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V121__seed_coco_daily_cap.sql" in _read(rel)
