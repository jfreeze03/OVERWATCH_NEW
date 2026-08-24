"""CoCo efficiency + coaching-flag logic (Cost & Contract ▸ Chargeback & AI)."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.logic.wave2 import coco_coaching_count, coco_efficiency, token_economics


def _token_rows(user: str, inp: int, out: int, cread: int, cwrite: int) -> list[dict]:
    return [
        {"USER_NAME": user, "TOKEN_TYPE": "input", "TOKENS": inp},
        {"USER_NAME": user, "TOKEN_TYPE": "output", "TOKENS": out},
        {"USER_NAME": user, "TOKEN_TYPE": "cache_read_input", "TOKENS": cread},
        {"USER_NAME": user, "TOKEN_TYPE": "cache_write_input", "TOKENS": cwrite},
    ]


def _econ() -> pd.DataFrame:
    tt = pd.DataFrame(
        _token_rows("CRUTCH", 2_000_000, 700_000, 180_000_000, 18_000_000)
        + _token_rows("MID", 500_000, 300_000, 20_000_000, 1_500_000)
        + _token_rows("LIGHT", 300_000, 200_000, 5_000_000, 400_000)
    )
    return token_economics(tt)


def _daily() -> pd.DataFrame:
    rows: list[dict] = []
    base = dt.date(2026, 8, 1)
    for i in range(20):
        d = str(base + dt.timedelta(days=i))
        rows.append({"USER_NAME": "CRUTCH", "USAGE_DATE": d, "REQUESTS": 30, "CREDITS": 28.0})
        rows.append({"USER_NAME": "MID", "USAGE_DATE": d, "REQUESTS": 60, "CREDITS": 9.0})
        rows.append({"USER_NAME": "LIGHT", "USAGE_DATE": d, "REQUESTS": 40, "CREDITS": 3.0})
    return pd.DataFrame(rows)


def test_crutch_user_is_flagged_and_supplements_are_not():
    eff = coco_efficiency(_econ(), _daily(), cap_credits=15.0)
    by_user = eff.set_index("USER_NAME")
    assert by_user.loc["CRUTCH", "FLAG"] == "🚩 Review"
    assert by_user.loc["MID", "FLAG"] == ""
    assert by_user.loc["LIGHT", "FLAG"] == ""
    # the reason names the tripped, peer-relative signals — a defensible basis to coach
    assert "median spend" in by_user.loc["CRUTCH", "REASON"]
    assert "over 15cr" in by_user.loc["CRUTCH", "REASON"]
    assert coco_coaching_count(eff) == 1
    # flagged rows sort first
    assert eff.iloc[0]["USER_NAME"] == "CRUTCH"


def test_days_over_cap_counts_days_above_the_configured_cap():
    eff = coco_efficiency(_econ(), _daily(), cap_credits=15.0).set_index("USER_NAME")
    assert eff.loc["CRUTCH", "DAYS_OVER_CAP"] == 20   # 28cr/day > 15 on all 20 days
    assert eff.loc["MID", "DAYS_OVER_CAP"] == 0        # 9cr/day never over 15
    # raise the cap to 30 (the exception some users get) and CRUTCH's 28cr no longer counts
    hi = coco_efficiency(_econ(), _daily(), cap_credits=30.0).set_index("USER_NAME")
    assert hi.loc["CRUTCH", "DAYS_OVER_CAP"] == 0


def test_cache_write_share_and_read_amplification_are_derived():
    eff = coco_efficiency(_econ(), _daily(), cap_credits=15.0).set_index("USER_NAME")
    # cache-write % of full-price (input+output+cache_write): CRUTCH 18M/(2M+0.7M+18M) ~ 87%
    assert 85.0 <= eff.loc["CRUTCH", "CACHE_WRITE_PCT"] <= 89.0
    # read-amp = cache_read / (input+output): CRUTCH 180M/2.7M ~ 66.7x
    assert eff.loc["CRUTCH", "READ_AMP"] > eff.loc["LIGHT", "READ_AMP"]


def test_empty_and_missing_daily_degrade_without_raising():
    assert coco_efficiency(pd.DataFrame(), None).empty
    assert coco_efficiency(None, None).empty
    # token grain present but no daily -> cache metrics compute, but no credit-based flags
    eff = coco_efficiency(_econ(), None, cap_credits=15.0)
    assert coco_coaching_count(eff) == 0
    assert (eff["CACHE_WRITE_PCT"] > 0).any()   # cache-grain still derived
    assert (eff["DAYS_OVER_CAP"] == 0).all()
    # economics missing but daily present -> credit-only users still surface and can be flagged
    credit_only = coco_efficiency(None, _daily(), cap_credits=15.0)
    assert not credit_only.empty
    assert credit_only.set_index("USER_NAME").loc["CRUTCH", "FLAG"] == "🚩 Review"
    assert (credit_only["CACHE_WRITE_PCT"] == 0).all()   # no token grain -> cache cols zero


def test_a_sporadic_burst_is_not_flagged_as_a_chronic_crutch():
    # SPORADIC uses CoCo on only 2 days at high credits; NORMAL1/2 use it every day at ~5cr.
    # Its per-active-day intensity and cr/request are huge, but sustained spend is low and it is
    # over cap on only 2 days (< 5) — the over-cap GATE must keep it unflagged (the P1 fix).
    econ = token_economics(pd.DataFrame(
        _token_rows("SPORADIC", 400_000, 200_000, 8_000_000, 800_000)
        + _token_rows("NORMAL1", 900_000, 400_000, 30_000_000, 3_000_000)
        + _token_rows("NORMAL2", 850_000, 380_000, 28_000_000, 2_800_000)
    ))
    rows: list[dict] = []
    base = dt.date(2026, 8, 1)
    for i in range(20):
        d = str(base + dt.timedelta(days=i))
        rows.append({"USER_NAME": "NORMAL1", "USAGE_DATE": d, "REQUESTS": 50, "CREDITS": 6.0})
        rows.append({"USER_NAME": "NORMAL2", "USAGE_DATE": d, "REQUESTS": 50, "CREDITS": 5.0})
    rows.extend({"USER_NAME": "SPORADIC", "USAGE_DATE": d, "REQUESTS": 4, "CREDITS": 40.0}
                for d in ("2026-08-19", "2026-08-20"))
    eff = coco_efficiency(econ, pd.DataFrame(rows), cap_credits=15.0).set_index("USER_NAME")
    assert eff.loc["SPORADIC", "FLAG"] == ""          # 2 over-cap days < 5 -> gate blocks the flag
    assert eff.loc["SPORADIC", "ACTIVE_DAYS"] == 2     # transparent: only 2 active days
    assert eff.loc["SPORADIC", "DAYS_OVER_CAP"] == 2
    assert coco_coaching_count(eff) == 0


def test_never_raises_on_frames_missing_expected_columns():
    # a daily frame that passes the USER_NAME+USAGE_DATE guard but lacks CREDITS/REQUESTS
    thin_daily = pd.DataFrame({"USER_NAME": ["A"], "USAGE_DATE": ["2026-08-01"]})
    assert not coco_efficiency(None, thin_daily).empty            # surfaces A, no crash
    # an economics frame with USER_NAME but no token-type columns
    thin_econ = pd.DataFrame({"USER_NAME": ["A"]})
    out = coco_efficiency(thin_econ, None)
    assert list(out["USER_NAME"]) == ["A"]                        # no crash, cache metrics zeroed
    assert coco_coaching_count(out) == 0


def test_multi_source_day_counts_as_one_day_for_the_cap_test():
    # a user with two SOURCE rows on the same day (Snowsight + CLI) whose combined credits
    # cross the cap must count as ONE over-cap day, not two.
    econ = token_economics(pd.DataFrame(_token_rows("U", 100, 100, 100, 100)))
    daily = pd.DataFrame([
        {"USER_NAME": "U", "USAGE_DATE": "2026-08-01", "SOURCE": "CLI", "REQUESTS": 5, "CREDITS": 10.0},
        {"USER_NAME": "U", "USAGE_DATE": "2026-08-01", "SOURCE": "Snowsight", "REQUESTS": 5, "CREDITS": 10.0},
    ])
    eff = coco_efficiency(econ, daily, cap_credits=15.0).set_index("USER_NAME")
    assert eff.loc["U", "DAYS_OVER_CAP"] == 1   # 20cr combined on the one day, not 2 days


def test_as_of_anchors_the_window_to_the_account_date():
    # Metering lags: the latest USAGE_DATE (today-2) predates the account date. Anchoring the
    # window on max(USAGE_DATE) silently shifts it back and counts days that fall outside the
    # requested [as_of - window] range; passing as_of pins the window to the real 'today' so it
    # reconciles with the AI-users tab (which slices off account_today()).
    today = dt.date(2026, 8, 24)
    daily = pd.DataFrame([
        {"USER_NAME": "U", "USAGE_DATE": str(today - dt.timedelta(days=2 + i)),
         "REQUESTS": 5, "CREDITS": 20.0}
        for i in range(10)
    ])
    econ = token_economics(pd.DataFrame(_token_rows("U", 100, 100, 100, 100)))
    anchored = coco_efficiency(econ, daily, cap_credits=15.0, window_days=3, as_of=today)
    drifting = coco_efficiency(econ, daily, cap_credits=15.0, window_days=3)
    a = anchored.set_index("USER_NAME").loc["U", "DAYS_OVER_CAP"]
    b = drifting.set_index("USER_NAME").loc["U", "DAYS_OVER_CAP"]
    assert a == 2      # only today-2 and today-3 fall within [today-3, today]
    assert b == 4      # max-anchor drifts the window back to [today-5, today-2]
    assert a < b


def test_dominant_user_flags_in_a_two_user_company_scope():
    # Leave-one-out peer median (R2-3): a whole-population median includes the user being tested,
    # so in a 1-2 user company scope the heaviest user's ratio to the midpoint is < 2 and the
    # flag could NEVER fire. A chronically over-cap dominant user in a 2-user scope must flag.
    today = dt.date(2026, 8, 24)
    rows = [
        {"USER_NAME": "HEAVY", "USAGE_DATE": str(today - dt.timedelta(days=1 + i)),
         "REQUESTS": 2, "CREDITS": 25.0}
        for i in range(30)
    ]
    rows.append({"USER_NAME": "LIGHT", "USAGE_DATE": str(today - dt.timedelta(days=1)),
                 "REQUESTS": 1, "CREDITS": 5.0})
    daily = pd.DataFrame(rows)
    econ = token_economics(pd.DataFrame(_token_rows("HEAVY", 100, 100, 100, 100)
                                        + _token_rows("LIGHT", 10, 10, 10, 10)))
    eff = coco_efficiency(econ, daily, cap_credits=15.0, window_days=30, as_of=today).set_index("USER_NAME")
    assert eff.loc["HEAVY", "FLAG"] == "🚩 Review"
    assert eff.loc["HEAVY", "DAYS_OVER_CAP"] == 30
    assert eff.loc["HEAVY", "PEER_MULT"] >= 2.0
    assert eff.loc["LIGHT", "FLAG"] == ""          # under cap -> never flagged
    assert coco_coaching_count(eff.reset_index()) == 1


def test_zero_credit_users_do_not_drag_the_peer_baseline_to_zero():
    # A zero-credit user is not a spending peer. If zeros stay in the leave-one-out baseline its
    # median can collapse to 0 and (via the med>0 guard) silently drop a genuinely dominant heavy
    # user's flag. positive_baseline must exclude them so HEAVY still flags.
    today = dt.date(2026, 8, 24)
    rows = [
        {"USER_NAME": "HEAVY", "USAGE_DATE": str(today - dt.timedelta(days=1 + i)),
         "REQUESTS": 2, "CREDITS": 25.0}
        for i in range(30)
    ]
    rows.append({"USER_NAME": "LIGHT", "USAGE_DATE": str(today - dt.timedelta(days=1)),
                 "REQUESTS": 1, "CREDITS": 5.0})
    rows.extend(       # zero-credit users present in the scan
        {"USER_NAME": u, "USAGE_DATE": str(today - dt.timedelta(days=1)),
         "REQUESTS": 1, "CREDITS": 0.0}
        for u in ("Z1", "Z2"))
    daily = pd.DataFrame(rows)
    econ = token_economics(pd.DataFrame(_token_rows("HEAVY", 100, 100, 100, 100)))
    eff = coco_efficiency(econ, daily, cap_credits=15.0, window_days=30, as_of=today).set_index("USER_NAME")
    assert eff.loc["HEAVY", "FLAG"] == "🚩 Review"
    assert eff.loc["HEAVY", "PEER_MULT"] >= 2.0


def test_scoped_view_does_not_fall_back_to_account_wide_token_population():
    # A company view whose credit rows are empty (daily scan didn't resolve, or all rows fall
    # outside the window) must NOT fall back to the account-wide token grain -- that would list
    # other companies' users under the company. scoped=True returns empty; the ALL view (scoped=
    # False) keeps its correct account-wide fallback.
    econ = token_economics(pd.DataFrame(_token_rows("OTHERCO_USER", 100, 100, 100, 100)))
    assert coco_efficiency(econ, None, cap_credits=15.0, window_days=30, scoped=True).empty
    unscoped = coco_efficiency(econ, None, cap_credits=15.0, window_days=30, scoped=False)
    assert list(unscoped["USER_NAME"]) == ["OTHERCO_USER"]
