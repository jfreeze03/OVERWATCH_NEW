"""CoCo CR#4 (duration, not count): oldest undelivered-critical age in the banners."""
from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_health_strip_emits_undelivered_oldest_age():
    sql = mart_sql.health_strip()
    assert "'UNDELIVERED_OLDEST_MIN'" in sql        # the surfaced metric
    assert "AS UNDELIVERED_OLDEST_MIN" in sql        # the crit-CTE age column
    # still keyed off the same undelivered predicate (OPEN, 30m+ old, no delivery row)
    assert "DATEDIFF('minute', e.RAISED_AT, CURRENT_TIMESTAMP())" in sql


def test_reached_nobody_banners_show_the_age():
    for rel in ("control_room.py", "brief.py"):
        src = (_ROOT / "app" / "ui" / "pages" / rel).read_text(encoding="utf-8")
        assert "UNDELIVERED_OLDEST_MIN" in src
        assert "humanize_duration(" in src and "oldest" in src
