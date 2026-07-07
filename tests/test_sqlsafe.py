import pytest

from app.core.sqlsafe import (
    assert_no_control_tokens,
    clean_filter_text,
    contains_filter,
    in_list,
    like_any,
    not_in_list,
    safe_identifier,
    sql_literal,
    sql_number,
)


def test_sql_literal_escapes_quotes():
    assert sql_literal("O'Brien") == "'O''Brien'"
    assert sql_literal(None) == "NULL"


def test_sql_literal_strips_nul_and_caps_length():
    assert sql_literal("a\x00b", 10) == "'ab'"
    assert sql_literal("x" * 50, 10) == "'" + "x" * 10 + "'"


def test_sql_number_never_interpolates_text():
    assert sql_number("12.5") == "12.5"
    assert sql_number("12; DROP TABLE x", default=3.0) == "3.0"


def test_safe_identifier_accepts_qualified_when_allowed():
    assert safe_identifier("OVERWATCH.CORE.SETTINGS", allow_qualified=True) == "OVERWATCH.CORE.SETTINGS"
    with pytest.raises(ValueError):
        safe_identifier("OVERWATCH.CORE.SETTINGS")  # dots not allowed unqualified
    with pytest.raises(ValueError):
        safe_identifier("bad-name")
    with pytest.raises(ValueError):
        safe_identifier("x; DROP TABLE y")
    with pytest.raises(ValueError):
        safe_identifier("")


def test_clean_filter_text_degrades_to_off():
    assert clean_filter_text("WH_TRXS%") == "WH_TRXS%"
    assert clean_filter_text("Robert'); DROP TABLE users;--") == ""
    assert clean_filter_text("SELECT * FROM x") == ""
    assert clean_filter_text(None) == ""


def test_contains_filter_wraps_wildcards():
    clause = contains_filter("USER_NAME", "kebarr")
    assert clause == "USER_NAME ILIKE '%kebarr%'"
    assert contains_filter("USER_NAME", "x; DROP") == ""


def test_assert_no_control_tokens_masks_literals():
    # Control words inside quoted literals are fine…
    assert_no_control_tokens("COL = 'DROP TABLE'")
    # …but bare control tokens are rejected.
    with pytest.raises(ValueError):
        assert_no_control_tokens("1=1; DROP TABLE x")
    with pytest.raises(ValueError):
        assert_no_control_tokens("col = 1 UNION SELECT password")


def test_list_builders():
    assert in_list("W", ["a", "b"]) == "UPPER(W) IN ('A', 'B')"
    assert in_list("W", []) == ""
    assert not_in_list("W", ["a"]) == "(W IS NULL OR UPPER(W) NOT IN ('A'))"
    assert not_in_list("W", ["a"], allow_null=False) == "UPPER(W) NOT IN ('A')"
    mixed = like_any("D", ["EXACT", "PREF%"])
    assert "UPPER(D) IN ('EXACT')" in mixed and "D ILIKE 'PREF%'" in mixed
