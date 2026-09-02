"""Owner error-log fixes 2026-08-17 (full messages from APP_ERROR_LOG):
- Alerts 000904 invalid identifier RESOLUTION_NOTE (x13): read the note from
  ALERT_AUDIT, not ALERT_EVENTS (which never had that column).
- Cost 001072 Lateral View cannot be on the left side of join: the TOKENS_GRANULAR
  builder put FLATTEN left of a LEFT JOIN (a wave-2 regression).
- Alerts 002031 Unsupported subquery type: incident_metrics nested a scalar
  subquery inside NULLIF inside a scalar subquery."""

from __future__ import annotations

import pytest

from app.data import cortex_sql, mart_sql

sqlglot = pytest.importorskip("sqlglot")


def _parses(sql: str) -> bool:
    sqlglot.parse_one(sql, dialect="snowflake")
    return True


def test_resolutions_for_rule_reads_note_from_alert_audit():
    sql = mart_sql.resolutions_for_rule("PERF_QUERY_FAIL_PCT")
    assert _parses(sql)
    # the note now comes from ALERT_AUDIT (joined on EVENT_ID), not a bare
    # ALERT_EVENTS.RESOLUTION_NOTE column (which does not exist -> 000904).
    assert "ALERT_AUDIT" in sql
    assert "a.NOTE AS RESOLUTION_NOTE" in sql
    assert "MAX_BY(NOTE, ACTED_AT)" in sql
    # ALERT_EVENTS (aliased e) is the driving table but is never asked for a
    # RESOLUTION_NOTE column (it has none — that was the 000904).
    assert "e.RESOLUTION_NOTE" not in sql


def test_token_types_flatten_is_not_left_of_join():
    sql = cortex_sql.cortex_code_token_types(30)
    assert _parses(sql)
    # FLATTEN lives in its own CTE; the LEFT JOIN USERS is on that CTE, so no
    # LATERAL view sits on the left of the join (001072).
    assert "flat AS (" in sql
    assert "FROM flat\nLEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS" in sql
    # the old broken shape (comma-join + LATERAL then LEFT JOIN) is gone.
    assert "LATERAL FLATTEN(INPUT => C.TOKENS_GRANULAR) F\nLEFT JOIN" not in sql


def test_incident_metrics_has_no_nested_scalar_subquery():
    sql = mart_sql.incident_metrics(30)
    assert _parses(sql)
    # the 002031 trigger — a scalar subquery nested inside NULLIF inside a scalar
    # subquery — is gone; counts are hoisted to CTEs and cross-joined.
    assert "NULLIF((SELECT" not in sql
    assert "wn AS (SELECT COUNT(*) AS N FROM w)" in sql
    assert "FROM wn CROSS JOIN compression" in sql        # reopen CTE dropped (LBA-1, below)
    # semantics preserved: the surviving output columns. Three dead metrics dropped, each
    # counting a column no writer persists: CHANGE_PCT (v4.351), MTTA_MIN (ALC-2:
    # INCIDENTS.ACK_AT never set), REOPEN_PCT (ALC-1: INCIDENTS.REOPENED_FROM never set).
    for col in ("OPEN_NOW", "DECLARED_N", "TTD_MIN", "MTTR_MIN", "COMPRESSION"):
        assert f"AS {col}" in sql, col
    assert "CHANGE_PCT" not in sql
    assert "MTTA_MIN" not in sql and "REOPEN_PCT" not in sql and "reopen" not in sql
