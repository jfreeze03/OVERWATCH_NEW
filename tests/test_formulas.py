from datetime import date

from app.logic.formulas import (
    allocate_by_share,
    billed_credits,
    credits_to_usd,
    format_credits,
    format_usd,
    month_days,
    pct_delta,
    safe_div,
    safe_float,
)


def test_credits_to_usd_default_rate_is_contract_rate():
    assert credits_to_usd(100) == 368.00
    assert credits_to_usd(1, 2.20) == 2.20


def test_credits_to_usd_garbage_is_zero():
    assert credits_to_usd(None) == 0.0
    assert credits_to_usd(float("nan")) == 0.0


def test_billed_credits_applies_negative_adjustment():
    assert billed_credits(100.0, -7.5) == 92.5


def test_billed_credits_defensive_on_positive_adjustment():
    # Source column is documented <= 0; a positive value is treated as a rebate.
    assert billed_credits(100.0, 7.5) == 92.5


def test_billed_credits_never_negative():
    assert billed_credits(1.0, -5.0) == 0.0


def test_pct_delta_none_without_prior():
    assert pct_delta(50, 0) is None
    assert pct_delta(110, 100) == 10.0
    assert pct_delta(90, 100) == -10.0


def test_allocate_by_share_sums_to_total():
    parts = allocate_by_share(100.0, [1, 1, 2])
    assert parts == [25.0, 25.0, 50.0]


def test_allocate_by_share_zero_weights():
    assert allocate_by_share(100.0, [0, 0]) == [0.0, 0.0]


def test_month_days_february_leap():
    days, elapsed, remaining = month_days(date(2028, 2, 10))
    assert (days, elapsed, remaining) == (29, 10, 19)


def test_safe_div_and_float():
    assert safe_div(10, 0, default=-1) == -1
    assert safe_float("3.5") == 3.5
    assert safe_float("nope", 9.9) == 9.9


def test_formatting():
    assert format_usd(1_250_000) == "$1.25M"
    assert format_usd(12_345) == "$12,345"
    assert format_usd(9.5) == "$9.50"
    assert format_credits(1234) == "1,234"
    assert format_credits(0.1234) == "0.1234"
