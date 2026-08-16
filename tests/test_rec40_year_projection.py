"""rec#40: the calendar-year strip prorates today's remainder so the year-end
projection is constant through the day instead of running low all morning.
"""

from app.ui.pages.cost_parts.contract import _year_end_credits


def test_year_end_projection_is_constant_through_the_day():
    # Same warehouse, same day, two clock readings. YTD tracks the burn as the
    # day fills: start-of-day YTD excludes today (frac_left=1); end-of-day YTD
    # has today's full burn (frac_left=0). Both must land on the SAME year-end.
    burn, days_left = 10.0, 100
    start_of_day = _year_end_credits(1000.0, burn, frac_left=1.0, days_left=days_left)
    end_of_day = _year_end_credits(1010.0, burn, frac_left=0.0, days_left=days_left)
    midday = _year_end_credits(1005.0, burn, frac_left=0.5, days_left=days_left)
    assert start_of_day == end_of_day == midday == 1000.0 + 10.0 * 101


def test_year_end_counts_today_as_a_full_projected_day():
    # today (frac_left + the days after it) = days_left + 1 full days at burn.
    assert _year_end_credits(0.0, 5.0, frac_left=1.0, days_left=0) == 5.0     # only today left
    assert _year_end_credits(500.0, 5.0, frac_left=0.0, days_left=0) == 500.0  # today already spent


def test_year_end_zero_burn_is_flat_ytd():
    assert _year_end_credits(1234.0, 0.0, frac_left=0.7, days_left=50) == 1234.0
