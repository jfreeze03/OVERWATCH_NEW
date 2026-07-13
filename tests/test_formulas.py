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

def test_mtd_pace_vs_prior_month_same_days_basis():
    import datetime as dt

    import pandas as pd

    from app.logic.formulas import mtd_pace_vs_prior_month
    today = dt.date(2026, 7, 10)
    days = ([{"DAY": dt.date(2026, 6, d), "USD": 100.0} for d in range(1, 31)]
            + [{"DAY": dt.date(2026, 7, d), "USD": 150.0} for d in range(1, 11)])
    mtd, prior, pct = mtd_pace_vs_prior_month(pd.DataFrame(days), today)
    assert mtd == 1500.0 and prior == 1000.0            # first 10 days each
    assert round(pct, 1) == 50.0


def test_mtd_pace_caps_at_short_prior_months_and_never_fakes_zero():
    import datetime as dt

    import pandas as pd

    from app.logic.formulas import mtd_pace_vs_prior_month
    # March 30th vs February: the span caps at Feb's length (28 in 2026)
    today = dt.date(2026, 3, 30)
    days = ([{"DAY": dt.date(2026, 2, d), "USD": 10.0} for d in range(1, 29)]
            + [{"DAY": dt.date(2026, 3, d), "USD": 10.0} for d in range(1, 31)])
    mtd, prior, _pct = mtd_pace_vs_prior_month(pd.DataFrame(days), today)
    assert prior == 280.0 and mtd == 300.0
    # no prior-month rows -> None, never a fabricated 0%
    only_now = pd.DataFrame([{"DAY": dt.date(2026, 7, 1), "USD": 5.0}])
    _, _, pct2 = mtd_pace_vs_prior_month(only_now, dt.date(2026, 7, 2))
    assert pct2 is None
    assert mtd_pace_vs_prior_month(None, today) == (0.0, 0.0, None)

