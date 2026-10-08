"""v4.612.0: the pure folds behind Cost > Chargeback & AI > Cortex Code models (app.logic.cortex.coco_*).

Synthetic frames in the builder's shape (cortex_sql.coco_model_usage_daily). What these pin: the window is the
AI users tab's own slice; the model rows always add back to the request totals (a residual is a visible pseudo
row, never a drop); "which model they select" is the main-model request count, additive, while "requests billing
it" is not; every ratio is NaN, never a ZeroDivisionError or a fabricated 0, when it has no denominator."""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from app.logic import cortex
from app.logic.cortex import (
    COCO_NO_BREAKDOWN,
    COCO_OVER_ATTRIBUTED,
    COCO_PSEUDO_MODELS,
    COCO_RESIDUAL_MIN_CREDITS,
    COCO_UNATTRIBUTED,
    coco_breakdown,
    coco_data_from,
    coco_kpis,
    coco_model_mix,
    coco_model_users,
    coco_model_window,
    coco_user_rollup,
    coco_window_start,
)
from app.logic.wave2 import cache_hit_pct, token_economics

_TODAY = date(2026, 8, 20)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(cortex, "account_today", lambda: _TODAY)


def _row(day, user, model, *, source="CLI", role="R", using=1, main=1, req_cr=0.0, cr=0.0, cr_input=None,
         tin=0.0, tread=0.0, tout=0.0, first="2026-08-15 15:00:00+00:00", last="2026-08-15 16:00:00+00:00"):
    return {
        "USAGE_DATE": day, "USER_NAME": user, "SOURCE": source, "ROLE_NAME": role, "MODEL_NAME": model,
        "REQUESTS_USING": using, "MAIN_REQUESTS": main, "REQUEST_TOKEN_CREDITS": req_cr, "REQUEST_TOKENS": 0,
        "COCO_CREDITS": cr, "COCO_CREDITS_INPUT": cr if cr_input is None else cr_input,
        "COCO_CREDITS_CACHE_READ": 0.0, "COCO_CREDITS_CACHE_WRITE": 0.0, "COCO_CREDITS_OUTPUT": 0.0,
        "COCO_CREDITS_OTHER": 0.0, "TOKENS_INPUT": tin, "TOKENS_CACHE_READ": tread, "TOKENS_CACHE_WRITE": 0.0,
        "TOKENS_OUTPUT": tout, "TOKENS_OTHER": 0.0,
        "FIRST_TS": pd.Timestamp(first) if first else pd.NaT, "LAST_TS": pd.Timestamp(last) if last else pd.NaT,
    }


def _frame(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


# A two-model request: opus is its main model (most credits), haiku a helper it also billed. Then one cheap
# haiku-only request, and one request with no model breakdown (Desktop).
_D1, _D2 = date(2026, 8, 15), date(2026, 8, 16)


def _alice() -> pd.DataFrame:
    return _frame(
        _row(_D1, "ALICE", "claude-opus", using=1, main=1, req_cr=1.0, cr=0.9, tin=100, tread=300),
        _row(_D1, "ALICE", "claude-haiku", using=2, main=1, req_cr=0.1, cr=0.2, tin=50, tread=50),
        _row(_D1, "ALICE", COCO_NO_BREAKDOWN, source="Desktop", using=1, main=1, req_cr=0.3, cr=0.0),
    )


# ---- window ----------------------------------------------------------------------------------------------

def test_window_is_the_ai_users_slice_trailing_and_last_month():
    df = _frame(_row(date(2026, 7, 20), "A", "m", req_cr=1.0, cr=1.0),     # today - 31
                _row(date(2026, 7, 21), "A", "m", req_cr=2.0, cr=2.0),     # today - 30: the first day kept
                _row(date(2026, 8, 1), "A", "m", req_cr=4.0, cr=4.0))
    trailing = coco_model_window(df, 30)
    assert sorted(trailing["USAGE_DATE"]) == [date(2026, 7, 21), date(2026, 8, 1)]
    assert list(trailing.index) == [0, 1]
    july = coco_model_window(df, 31, bounds=(date(2026, 7, 1), date(2026, 8, 1)))   # end exclusive
    assert sorted(july["USAGE_DATE"]) == [date(2026, 7, 20), date(2026, 7, 21)]
    assert coco_window_start(30) == date(2026, 7, 21)
    assert coco_window_start(31, bounds=(date(2026, 7, 1), date(2026, 8, 1))) == date(2026, 7, 1)
    assert coco_data_from(df) == date(2026, 7, 20) and coco_data_from(pd.DataFrame()) is None


def test_window_coerces_measures_and_never_invents_a_missing_column():
    df = _frame(_row(_D1, "A", None, req_cr=None, cr=None, cr_input=None))
    win = coco_model_window(df, 30)
    assert win.loc[0, "REQUEST_TOKEN_CREDITS"] == 0.0 and win.loc[0, "COCO_CREDITS"] == 0.0
    assert win.loc[0, "MODEL_NAME"] == COCO_NO_BREAKDOWN
    with pytest.raises(KeyError):                         # no fabricated zeros for a column the read lacks
        coco_model_window(df.drop(columns=["COCO_CREDITS"]), 30)


def test_empty_in_gives_empty_out_with_columns():
    assert list(coco_model_window(None, 30).columns) == list(cortex._COCO_COLUMNS)
    assert coco_model_window(_alice(), 1).empty           # outside the window
    for out in (coco_model_mix(None), coco_model_mix(pd.DataFrame(), by=("USER_NAME",)), coco_user_rollup(None),
                coco_breakdown(None, "SOURCE"), coco_model_users(None, "m")):
        assert out.empty and len(out.columns) > 3
    assert coco_kpis(None)["desktop_share_pct"] is None and coco_kpis(None)["top_model"] is None


# ---- model mix -------------------------------------------------------------------------------------------

def test_model_rows_add_back_to_the_request_totals_with_a_visible_residual():
    win = coco_model_window(_alice(), 30)
    mix = coco_model_mix(win)
    assert math.isclose(mix["COCO_CREDITS"].sum(), win["REQUEST_TOKEN_CREDITS"].sum())        # 1.4 both
    # the no-breakdown request is folded into the unattributed row, which also carries the leaf gap
    assert COCO_NO_BREAKDOWN not in set(mix["MODEL_NAME"])
    unattr = mix[mix["MODEL_NAME"] == COCO_UNATTRIBUTED].iloc[0]
    assert math.isclose(unattr["COCO_CREDITS"], 1.4 - 1.1)
    assert unattr["USERS"] == 1 and unattr["MAIN_REQUESTS"] == 1
    assert math.isnan(unattr["CREDITS_PER_REQUEST"]) and math.isnan(unattr["CREDITS_PER_1M_TOKENS"])
    # shares of the request totals add to 100
    assert math.isclose(mix["SHARE_PCT"].sum(), 100.0)
    assert mix["MODEL_NAME"].iloc[0] == "claude-opus"                                            # by credits desc


def test_the_invariant_holds_per_group():
    df = pd.concat([_alice(), _frame(_row(_D2, "BOB", "claude-opus", source="Snowsight", req_cr=0.5, cr=0.7))])
    win = coco_model_window(df, 30)
    for by in (("USAGE_DATE",), ("USER_NAME",), ("USAGE_DATE", "SOURCE")):
        mix = coco_model_mix(win, by=by)
        got = mix.groupby(list(by))["COCO_CREDITS"].sum()
        want = win.groupby(list(by))["REQUEST_TOKEN_CREDITS"].sum()
        for key, value in want.items():
            assert abs(got[key] - value) < COCO_RESIDUAL_MIN_CREDITS, (by, key)
    # BOB's leaves are above his request total: the negative residual is its own labelled row
    bob = coco_model_mix(win, by=("USER_NAME",))
    over = bob[(bob["USER_NAME"] == "BOB") & (bob["MODEL_NAME"] == COCO_OVER_ATTRIBUTED)].iloc[0]
    assert math.isclose(over["COCO_CREDITS"], -0.2) and over["USERS"] == 1


@pytest.mark.parametrize("pseudo", [COCO_UNATTRIBUTED, COCO_OVER_ATTRIBUTED])
def test_a_pseudo_row_equals_the_sum_of_its_drill(pseudo):
    """Review r1: mixed-sign residuals never net into one smaller row, and many sub-minimum gaps never sum into a
    row with no users and an empty drill -- the summary row is the sum of coco_model_users for it."""
    rows = [_row(_D1, "A", COCO_NO_BREAKDOWN, req_cr=10.0, cr=0.0),                  # +10 unattributed
            _row(_D1, "B", "m", req_cr=1.0, cr=4.0)]                                   # -3 above the totals
    rows += [_row(_D1, f"U{i}", "m", req_cr=1.0, cr=1.0 - COCO_RESIDUAL_MIN_CREDITS * 0.8) for i in range(300)]
    win = coco_model_window(_frame(*rows), 30)
    mix = coco_model_mix(win)
    summary = mix[mix["MODEL_NAME"] == pseudo]
    drill = coco_model_users(win, pseudo)
    assert len(summary) == 1 and len(drill) == summary.iloc[0]["USERS"] == 1
    assert math.isclose(summary.iloc[0]["COCO_CREDITS"], drill["COCO_CREDITS"].sum())
    assert math.isclose(summary.iloc[0]["COCO_CREDITS"], 10.0 if pseudo == COCO_UNATTRIBUTED else -3.0)


def test_a_residual_below_the_minimum_adds_no_row():
    win = coco_model_window(_frame(_row(_D1, "A", "m", req_cr=1.0, cr=1.0 - COCO_RESIDUAL_MIN_CREDITS / 2)), 30)
    mix = coco_model_mix(win)
    assert list(mix["MODEL_NAME"]) == ["m"]
    assert not (set(mix["MODEL_NAME"]) & COCO_PSEUDO_MODELS)


def test_a_two_model_request_counts_once_as_main_and_twice_as_billing():
    # one request billed opus (main) and haiku (helper): the builder emits one row per model
    win = coco_model_window(_frame(_row(_D1, "A", "opus", using=1, main=1, req_cr=1.0, cr=0.8),
                                   _row(_D1, "A", "haiku", using=1, main=0, req_cr=0.0, cr=0.2)), 30)
    mix = coco_model_mix(win)
    assert mix["MAIN_REQUESTS"].sum() == 1            # additive: the request count
    assert mix["REQUESTS_USING"].sum() == 2           # not additive: it billed two models
    user = coco_user_rollup(win).iloc[0]
    assert user["REQUESTS"] == 1 and user["MOST_USED_MODEL"] == "opus" and user["MODELS_USED"] == 2


def test_cache_hit_is_the_one_shared_formula():
    win = coco_model_window(_alice(), 30)
    mix = coco_model_mix(win).set_index("MODEL_NAME")
    assert mix.loc["claude-opus", "CACHE_HIT_PCT"] == 75.0          # 300 / (300 + 100)
    assert math.isnan(mix.loc[COCO_UNATTRIBUTED, "CACHE_HIT_PCT"])   # no prompt tokens: no hit rate
    econ = token_economics(pd.DataFrame({"USER_NAME": ["A", "A", "B"],
                                         "TOKEN_TYPE": ["input", "cache_read_input", "output"],
                                         "TOKENS": [100.0, 300.0, 5.0]})).set_index("USER_NAME")
    assert econ.loc["A", "CACHE_HIT_PCT"] == 75.0 and math.isnan(econ.loc["B", "CACHE_HIT_PCT"])
    got = cache_hit_pct(pd.Series([300.0, 0.0]), pd.Series([100.0, 0.0]))
    assert got.iloc[0] == 75.0 and math.isnan(got.iloc[1])


# ---- per user --------------------------------------------------------------------------------------------

def test_user_rollup_top_model_by_dollars_vs_most_used_by_requests():
    win = coco_model_window(pd.concat([
        _alice(),
        _frame(_row(_D2, "ALICE", "claude-haiku", using=3, main=3, req_cr=0.15, cr=0.15, source="CLI",
                    first="2026-08-16 01:00:00+00:00", last="2026-08-17 02:30:00+00:00")),
    ]), 30)
    user = coco_user_rollup(win).iloc[0]
    assert user["TOP_MODEL"] == "claude-opus"                  # most credits
    assert user["MOST_USED_MODEL"] == "claude-haiku"           # most main-model requests: 4 of 6
    assert user["REQUESTS"] == 6 and math.isclose(user["MOST_USED_SHARE_PCT"], 4 / 6 * 100)
    assert math.isclose(user["TOP_MODEL_SHARE_PCT"], 0.9 / 1.55 * 100)
    assert user["MODELS_USED"] == 2                            # pseudo rows are not models
    assert user["INTERFACES"] == "CLI, Desktop" and user["ACTIVE_DAYS"] == 2
    assert user["CACHE_HIT_PCT"] == round(350 / 500 * 100, 1)
    # timestamps arrive tz-aware and are shown as account (Central) wall time
    assert user["FIRST_USED_AT"] == pd.Timestamp("2026-08-15 10:00:00")
    assert user["LAST_USED_AT"] == pd.Timestamp("2026-08-16 21:30:00")


def test_a_zero_spend_user_has_nan_shares_not_a_division_error():
    win = coco_model_window(_frame(_row(_D1, "Z", COCO_NO_BREAKDOWN, using=0, main=0, req_cr=0.0, cr=0.0)), 30)
    user = coco_user_rollup(win).iloc[0]
    assert user["TOKEN_CREDITS"] == 0 and user["MODELS_USED"] == 0
    assert pd.isna(user["TOP_MODEL"]) and pd.isna(user["MOST_USED_MODEL"])
    assert math.isnan(user["TOP_MODEL_SHARE_PCT"]) and math.isnan(user["MOST_USED_SHARE_PCT"])
    assert math.isnan(user["CACHE_HIT_PCT"])


def test_user_rollup_is_ranked_by_spend_and_uncapped():
    rows = [_row(_D1, f"U{i:03d}", "m", req_cr=float(i), cr=float(i)) for i in range(1, 601)]
    users = coco_user_rollup(coco_model_window(_frame(*rows), 30))
    assert len(users) == 600 and users.iloc[0]["USER_NAME"] == "U600"


def test_breakdowns_add_up_to_the_frame():
    df = pd.concat([_alice(), _frame(_row(_D2, "ALICE", "claude-opus", source="Snowsight", role="R2",
                                          req_cr=0.5, cr=0.5))])
    win = coco_model_window(df, 30)
    for dim in ("SOURCE", "ROLE_NAME"):
        part = coco_breakdown(win, dim)
        assert math.isclose(part["TOKEN_CREDITS"].sum(), win["REQUEST_TOKEN_CREDITS"].sum()), dim
        assert part["REQUESTS"].sum() == win["MAIN_REQUESTS"].sum(), dim
        assert math.isclose(part["SHARE_PCT"].sum(), 100.0), dim
    src = coco_breakdown(win, "SOURCE").set_index("SOURCE")
    assert src.loc["Desktop", "MODELS"] == 0 and src.loc["CLI", "MODELS"] == 2
    with pytest.raises(ValueError):
        coco_breakdown(win, "MODEL_NAME")


# ---- per model -------------------------------------------------------------------------------------------

def test_model_users_share_of_each_users_spend():
    df = pd.concat([_alice(), _frame(_row(_D2, "BOB", "claude-opus", source="Snowsight", req_cr=0.5, cr=0.5),
                                     _row(_D2, "BOB", "claude-haiku", main=1, req_cr=0.5, cr=0.5))])
    win = coco_model_window(df, 30)
    opus = coco_model_users(win, "claude-opus").set_index("USER_NAME")
    assert list(opus.index) == ["ALICE", "BOB"]                  # highest credits first
    assert math.isclose(opus.loc["ALICE", "SHARE_OF_USER_PCT"], 0.9 / 1.4 * 100)
    assert math.isclose(opus.loc["BOB", "SHARE_OF_USER_PCT"], 50.0)
    # the pseudo row lists each user's own unattributed credits (the builder literal maps to it too)
    gap = coco_model_users(win, COCO_NO_BREAKDOWN)
    assert list(gap["USER_NAME"]) == ["ALICE"] and math.isclose(gap.iloc[0]["COCO_CREDITS"], 0.3)
    assert coco_model_users(win, "no-such-model").empty


def test_kpis_split_interfaces_from_the_unfiltered_window():
    full = coco_model_window(pd.concat([_alice(), _frame(_row(_D2, "BOB", "claude-opus", source="Snowsight",
                                                              req_cr=0.6, cr=0.6))]), 30)
    cli_only = full[full["SOURCE"] == "CLI"]
    k = coco_kpis(cli_only, full)
    assert math.isclose(k["spend"], 1.1) and k["users"] == 1 and k["models_used"] == 2
    assert k["top_model"] == "claude-opus" and math.isclose(k["top_model_share_pct"], 0.9 / 1.1 * 100)
    assert math.isclose(k["snowsight_cli"], 1.7) and math.isclose(k["desktop"], 0.3)
    assert math.isclose(k["desktop_share_pct"], 0.3 / 2.0 * 100)                 # never the filtered window
    assert math.isclose(k["named_model_credits"], 1.1)
    zero = coco_model_window(_frame(_row(_D1, "A", "m", req_cr=0.0, cr=0.0)), 30)
    assert coco_kpis(zero)["desktop_share_pct"] is None and coco_kpis(zero)["top_model"] is None
