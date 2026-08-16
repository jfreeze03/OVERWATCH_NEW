"""CoCo Alerts #26: 'how was this resolved last time' in the alert drawer."""
from pathlib import Path

import pytest

from app.data import mart_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]


def test_resolutions_for_rule_shape_and_parse():
    sql = mart_sql.resolutions_for_rule("COST_DAILY_CREDITS")
    assert "ALERT_EVENTS" in sql
    assert "STATUS = 'RESOLVED'" in sql
    assert "RESOLUTION_NOTE" in sql and "RESOLUTION_KIND" in sql
    assert "'SUPERSEDED'" in sql                     # superseded closes excluded
    assert "RULE_ID = 'COST_DAILY_CREDITS'" in sql
    assert "LIMIT 5" in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_resolutions_for_rule_validates_id_and_clamps_limit():
    with pytest.raises(ValueError):
        mart_sql.resolutions_for_rule("bad; DROP TABLE X")
    assert "LIMIT 20" in mart_sql.resolutions_for_rule("R", limit=999)


def test_alert_drawer_offers_prior_resolutions():
    src = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert "resolutions_for_rule(" in src and "How this was resolved before" in src
