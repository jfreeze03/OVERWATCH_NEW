"""Guard for the shared null-render root fix (_coerce_object_numerics).

Snowpark returns a NULL-bearing Snowflake NUMBER/FLOAT column as OBJECT dtype (Decimal/float
cells + Python None), which the table machinery would (a) skip in _auto_formats so it never
humanizes and (b) render with the literal "None" instead of the em-dash. The fix coerces those
to a real numeric dtype BUT must never touch a zero-padded numeric STRING like an error code.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from pandas.api import types as ptypes

from app.ui.components import _clean_numeric_cell, _coerce_object_numerics


def test_null_bearing_number_column_becomes_numeric():
    # a Snowflake NUMBER(x,y) with a NULL -> object column of Decimal + None
    df = pd.DataFrame({"OP_TIME_PCT": pd.Series([Decimal("40.0"), None, Decimal("0.0")], dtype=object)})
    out = _coerce_object_numerics(df)
    assert ptypes.is_numeric_dtype(out["OP_TIME_PCT"])   # now formattable + na_rep reaches it
    assert out["OP_TIME_PCT"].isna().sum() == 1          # the None became NaN -> renders "—"
    assert df["OP_TIME_PCT"].dtype == object             # caller's frame untouched (CSV keeps raw)


def test_zero_padded_string_code_stays_text():
    # ERROR_CODE is VARCHAR '002043' — must NOT become 2043.0 (would drop the padding + mislead)
    df = pd.DataFrame({"ERROR_CODE": pd.Series(["002043", None, "090105"], dtype=object)})
    out = _coerce_object_numerics(df)
    assert not ptypes.is_numeric_dtype(out["ERROR_CODE"])
    assert out is df                                     # nothing to coerce -> same object, no copy
    assert list(out["ERROR_CODE"].dropna()) == ["002043", "090105"]


def test_mixed_text_column_is_left_alone():
    df = pd.DataFrame({"FINGERPRINT": pd.Series(["170cabe8", None, "4281bd46"], dtype=object)})
    out = _coerce_object_numerics(df)
    assert not ptypes.is_numeric_dtype(out["FINGERPRINT"])


def test_clean_numeric_cell_never_shows_a_six_decimal_tail():
    # a coerced ratio/score/id column matches no _auto_formats convention; without a clean
    # formatter the Styler default renders 6 decimals ("2.500000"). This is the belt.
    assert _clean_numeric_cell(2.5) == "2.5"
    assert _clean_numeric_cell(3.0) == "3"            # whole value -> no decimals
    assert _clean_numeric_cell(16.234) == "16.234"
    assert _clean_numeric_cell(2.4999998) == "2.5"    # trimmed, not "2.499800"
    assert _clean_numeric_cell(12345) == "12,345"     # comma-grouped whole
    assert "." not in _clean_numeric_cell(1000000.0)  # 1e6 whole -> "1,000,000"
    assert _clean_numeric_cell("not-a-number") == "not-a-number"   # never raises


def test_already_numeric_and_empty_frames_are_untouched():
    df = pd.DataFrame({"GB": [1.0, 2.0, 3.0]})           # float64 already
    assert _coerce_object_numerics(df) is df             # dtype != object -> skipped, same object
    assert _coerce_object_numerics(pd.DataFrame()).empty
