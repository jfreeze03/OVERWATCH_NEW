"""Locks for V141 — A1: move the 3 daily-grain cost alerts off the hourly scan onto the daily scan.

COST_STORAGE_SURGE / COST_SERVERLESS_CREEP / COST_EGRESS_SPIKE read daily-grain ACCOUNT_USAGE and
dedupe per day/week, so the hourly scan re-evaluated them 24x/day for no benefit. V141 re-derives
SP_ALERT_SCAN (hourly, minus those 3 arms; tally 16->13) and SP_ALERT_SCAN_DAILY (daily, plus those
3 arms; tally 6->9). The moved arm SQL is byte-identical; only the OPS_SCAN_DEGRADED denominators and
RETURN strings change. Same dedupe keys => same alerts, once/day.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V141 = (_MIG / "V141__alert_cadence_daily_cost_rules.sql").read_text(encoding="utf-8")
_V119 = (_MIG / "V119__alert_autoclear_hysteresis_fix.sql").read_text(encoding="utf-8")
_V137 = (_MIG / "V137__dq_recon_error_alert.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _arms_from_v119():
    h = _proc(_V119, "SP_ALERT_SCAN()")
    a1213 = h[h.index("    -- [12] COST_STORAGE_SURGE"):h.index("    -- [14] PIPE_COPY_FAILURES")]
    a19 = h[h.index("    -- [19] COST_EGRESS_SPIKE"):h.index("    -- [20] SEC_NEW_EXPOSURE")]
    return a1213, a19


def test_v141_guarded_and_ordered():
    assert "EXCEPTION (-20141" in _V141 and "IF (v < 140) THEN" in _V141
    assert "SELECT 141 AS VERSION" in _V141 and "WHERE VERSION = 141)" in _V141


def test_v141_hourly_is_v119_minus_the_three_arms():
    """Strong guard: the hourly proc is byte-identical to V119's except the 3
    removed arms and the two denominator strings."""
    h119 = _proc(_V119, "SP_ALERT_SCAN()")
    h141 = _proc(_V141, "SP_ALERT_SCAN()")
    a1213, a19 = _arms_from_v119()
    expected = (h119.replace(a1213, "").replace(a19, "")
                .replace("' of 16 alert rule block(s) failed this run'",
                         "' of 13 alert rule block(s) failed this run'")
                .replace("(16 - :fails) || '/16 rule blocks ok'",
                         "(13 - :fails) || '/13 rule blocks ok'"))
    assert expected != h119                 # the removals/edits actually matched
    assert h141 == expected


def test_v141_daily_is_v137_plus_the_three_arms():
    """Strong guard: the daily proc is byte-identical to V137's with the 3 arms
    inserted verbatim before the [17] add-on and the two tally strings bumped."""
    d137 = _proc(_V137, "SP_ALERT_SCAN_DAILY()")
    d141 = _proc(_V141, "SP_ALERT_SCAN_DAILY()")
    a1213, a19 = _arms_from_v119()
    anchor = "    -- [17] PIPE_REF_GAP"
    expected = (d137.replace(anchor, a1213 + a19 + anchor)
                .replace("' of 6 daily alert rule block(s) failed this run'",
                         "' of 9 daily alert rule block(s) failed this run'")
                .replace("'alert scan daily v1 (task/login/budget-pace/forecast/AI-creep/contract): ' || (6 - :fails) || '/6 rule blocks ok (daily)'",
                         "'alert scan daily v2 (V141: storage-surge/serverless-creep/egress-spike moved off the hourly scan): ' || (9 - :fails) || '/9 rule blocks ok (daily)'"))
    assert expected != d137
    assert d141 == expected


def test_v141_rules_moved_and_tallies_updated():
    h141 = _proc(_V141, "SP_ALERT_SCAN()")
    d141 = _proc(_V141, "SP_ALERT_SCAN_DAILY()")
    for rule in ("COST_STORAGE_SURGE", "COST_SERVERLESS_CREEP", "COST_EGRESS_SPIKE"):
        assert rule not in h141, f"{rule} still on the hourly scan"
        assert rule in d141, f"{rule} missing from the daily scan"
    # core-arm counts: hourly 16->13, daily 6->9 (add-ons [17]/[18] never increment fails)
    assert h141.count("fails := fails + 1") == 13
    assert d141.count("fails := fails + 1") == 9
    # both OPS_SCAN_DEGRADED denominators + RETURN tallies moved together
    assert "of 13 alert rule" in h141 and "/13 rule blocks ok" in h141
    assert "of 9 daily alert rule" in d141 and "/9 rule blocks ok (daily)" in d141
    assert "of 16" not in h141 and "of 6 daily" not in d141


def test_validate_and_docs_track_v141():
    val = _read("snowflake/validate.sql")
    assert "V001..V146 applied" in val and "VERSION BETWEEN 1 AND 146) = 146" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V141__alert_cadence_daily_cost_rules.sql" in _read(rel)


def test_v141_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 141 in _EXPECTED_MIGRATIONS


def test_v141_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V141):
        sqlglot.parse(statement, dialect="snowflake")
