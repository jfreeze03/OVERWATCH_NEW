"""SYSTEM$WAIT (sleep polling) signature + argument parser — app/logic/system_wait.py (v4.595).

One definition shared by the CS-driver classifier, the per-query advisor and the billed-family SQL
builder. The parity test is the load-bearing one: the SQL SLEEP_FLAG (window totals) and the Python
classification must agree on every statement, or the KPI totals and the table rows diverge.
"""

from __future__ import annotations

import math
import re

import pytest

from app.logic.system_wait import SLEEP_SQL_PATTERN, is_sleep_statement, is_sleep_text, wait_seconds

SLEEPS = [
    "select system$wait(10)",
    "SELECT SYSTEM$WAIT(10);",
    "CALL SYSTEM$WAIT(30)",
    "call system$wait(60)",
    "CALL SYSTEM$WAIT(1200)",
    "SYSTEM$WAIT (10)",
    "select system$wait(\n  10\n)",
    "CALL SYSTEM$WAIT(?)",
    "CALL SYSTEM$WAIT(:1)",
    "select system$wait(5, 'FORTNIGHTS')",
    "select system$wait(12",                                   # truncated sample text
    "BEGIN CALL SYSTEM$WAIT(1); CALL SYSTEM$WAIT(2); END",     # a loop body: two calls
    "select 1; select system$wait(3)",
]
NOT_SLEEPS = [
    "SELECT SYSTEM$WAIT_FOR_SERVICES(60, 'svc')",
    "select system$waitlist(3)",
    "CALL SYSTEM$SOME_NEW_INTERNAL(?)",
    "CALL MYSYSTEM$WAIT(3)",                                   # a longer identifier, not the builtin
    "CALL FOO_SYSTEM$WAIT(3)",
    "select 1",
    "",
]


@pytest.mark.parametrize("text", SLEEPS)
def test_sleep_text_positives(text):
    assert is_sleep_text(text)


@pytest.mark.parametrize("text", NOT_SLEEPS)
def test_sleep_text_negatives(text):
    assert not is_sleep_text(text)


@pytest.mark.parametrize("value", [None, float("nan"), 10, b"SYSTEM$WAIT(1)"])
def test_non_str_is_never_sleep(value):
    assert not is_sleep_text(value)
    assert wait_seconds(value) is None


@pytest.mark.parametrize("text,expected", [
    ("select system$wait(10)", 10.0),
    ("CALL SYSTEM$WAIT(30)", 30.0),
    ("CALL SYSTEM$WAIT(60)", 60.0),
    ("CALL SYSTEM$WAIT(1200)", 1200.0),
    ("select system$wait(1,'MINUTES')", 60.0),
    ("select system$wait(1,'minute')", 60.0),
    ("select system$wait( 500 , 'milliseconds' )", 0.5),
    ("select system$wait(2,'HOURS')", 7200.0),
    ("select system$wait(1,'DAY')", 86400.0),
    ("select system$wait(3,'SECOND')", 3.0),
    ("select system$wait(3,'Seconds')", 3.0),
    ("select system$wait(250,'MICROSECONDS')", 0.00025),
    ("select system$wait(5,'NANOSECONDS')", 5e-9),
    ("select system$wait(0.5)", 0.5),
    ("select system$wait(.5)", 0.5),
    ("select system$wait('10')", 10.0),
    ("SYSTEM$WAIT (10)", 10.0),
    ("select system$wait(\n  10\n)", 10.0),
    ("select system$wait(0)", 0.0),
])
def test_wait_seconds_values(text, expected):
    got = wait_seconds(text)
    assert got is not None and math.isclose(got, expected, rel_tol=1e-12, abs_tol=1e-15)


@pytest.mark.parametrize("text", [
    "CALL SYSTEM$WAIT(?)",                                     # bind variable
    "CALL SYSTEM$WAIT(:1)",
    "select system$wait(5, 'FORTNIGHTS')",                     # unknown unit
    "select system$wait(-1)",                                  # negative (Snowflake rejects it)
    "select system$wait(12",                                   # truncated
    "BEGIN CALL SYSTEM$WAIT(1); CALL SYSTEM$WAIT(2); END",     # more than one call
    "select system$wait(n)",                                   # an expression
    "select 1",
])
def test_wait_seconds_none(text):
    assert wait_seconds(text) is None


def test_ddl_that_contains_the_call_is_not_a_sleep():
    ddl = "CREATE OR REPLACE TASK T WAREHOUSE = W SCHEDULE = '5 MINUTE' AS CALL SYSTEM$WAIT(10)"
    assert is_sleep_text(ddl)                                  # the text contains it ...
    assert not is_sleep_statement(ddl, "CREATE_TASK")          # ... but defining a sleep is not sleeping
    assert not is_sleep_statement(ddl)                         # no type: the first keyword decides
    assert not is_sleep_statement("  alter task t modify as call system$wait(1)")
    assert not is_sleep_statement("ALTER TASK T MODIFY AS CALL SYSTEM$WAIT(1)", "ALTER_TASK")
    assert is_sleep_statement("CALL SYSTEM$WAIT(10)", "CALL")
    assert is_sleep_statement("select system$wait(10)", "SELECT")
    assert is_sleep_statement("select system$wait(10)")
    assert not is_sleep_statement("select 1", "SELECT")


ALL_TEXTS = SLEEPS + NOT_SLEEPS + [
    "CREATE OR REPLACE TASK T AS CALL SYSTEM$WAIT(10)",
    "select system$wait(1,'MINUTES')",
    "WITH x AS (SELECT 1) SELECT SYSTEM$WAIT(2) FROM x",
    "select system$wait\t(4)",
    "select system$wait\r\n(4)",
]


@pytest.mark.parametrize("text", ALL_TEXTS)
def test_sql_pattern_agrees_with_python(text):
    # SQL runs REGEXP_INSTR(UPPER(text), SLEEP_SQL_PATTERN); POSIX [[:space:]] == Python \s here.
    sql_re = re.compile(SLEEP_SQL_PATTERN.replace("[[:space:]]", r"\s"))
    assert (sql_re.search(text.upper()) is not None) == is_sleep_text(text)


def test_sql_pattern_is_literal_safe():
    # embedded in a SQL string literal: no backslash (escape) and no quote
    assert "\\" not in SLEEP_SQL_PATTERN and "'" not in SLEEP_SQL_PATTERN
