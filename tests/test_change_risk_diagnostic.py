"""Change-risk diagnostic (owner 2026-08-17): before writing any exclusion for
the Security CHANGE RISK DESTRUCTIVE flood, the app must SHOW which roles/objects
actually drive it (V080's fixed-role exclusion was outrun by unverified roles).
These lock the diagnostic SQL and its wiring into the Security overview."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import security_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_breakdown_counts_exactly_the_change_risk_destructive_rows():
    sql = security_sql.change_risk_destructive_breakdown(7)
    # Same rows the exception queue's CHANGE RISK arm counts.
    assert "FACT_SECURITY_CHANGE" in sql
    assert "CHANGE_KIND = 'DESTRUCTIVE'" in sql
    assert "RISK_SCORE >= 70" in sql
    assert "DATEADD('day', -7, CURRENT_TIMESTAMP())" in sql
    # Grouped by actor + object so the driver is visible.
    assert "ROLE_NAME" in sql and "DATABASE_NAME" in sql and "SCHEMA_NAME" in sql
    # Flags whether each role matches the TF_* service convention (literal underscore).
    assert "LIKE 'TF~_%' ESCAPE '~'" in sql
    assert "'TF_* service'" in sql


def test_breakdown_window_is_bounded():
    # A hostile / silly window can't scan the whole change history.
    big = security_sql.change_risk_destructive_breakdown(9999)
    assert "DATEADD('day', -30," in big  # bounded_days(.., 30) cap
    small = security_sql.change_risk_destructive_breakdown(1)
    assert "DATEADD('day', -1," in small


def test_diagnostic_is_wired_into_the_security_overview():
    ui = (_ROOT / "app" / "ui" / "security_center.py").read_text(encoding="utf-8")
    assert "change_risk_destructive_breakdown" in ui
    assert "_render_change_risk_diagnostic()" in ui
    # The summary reads the fields the SQL emits.
    for token in ("ROLE_CLASS", "TF_* service", "(no role attributed)", "DBA_MAINT_DB"):
        assert token in ui, token


def test_breakdown_parses_under_sqlglot():
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(security_sql.change_risk_destructive_breakdown(7), dialect="snowflake")
