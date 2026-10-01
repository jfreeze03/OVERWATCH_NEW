"""R1-104: Cortex Code cache metrics are unknown, not zero, where there is no token grain to compute them.

coco_efficiency's population is the credit set left-merged with the token-grain cache frame. A user with credits
but no TOKENS_GRANULAR rows (the flatten has no OUTER, so rows that predate the column produce credits only) got
CACHE_HIT_PCT 0.0 / READ_AMP 0 / CACHE_WRITE_PCT 0.0 -- rendered "Cache hit % 0.0%", the worst possible value --
beside the panel's "cache-hit is high across every user here" note. token_economics also wrote 0.0% for a user
with no prompt tokens. Those cells are now NaN ('—'); the credit signals and the review flag are unchanged.
"""

from __future__ import annotations

import pandas as pd

from app.logic.wave2 import coco_coaching_count, coco_efficiency, fleet_cache_hit_pct, token_economics


def _tokens(user: str, inp: float, out: float, read: float, write: float) -> list[dict]:
    return [{"USER_NAME": user, "TOKEN_TYPE": t, "TOKENS": v}
            for t, v in (("input", inp), ("output", out), ("cache_read_input", read), ("cache_write_input", write))]


def test_a_credit_user_with_no_token_grain_has_unknown_cache_metrics():
    econ = token_economics(pd.DataFrame(_tokens("A", 100, 50, 9000, 10)))
    daily = pd.DataFrame([{"USER_NAME": u, "USAGE_DATE": "2026-09-0" + str(d), "REQUESTS": 10, "CREDITS": c}
                          for u, c in (("A", 1.0), ("B", 8.0)) for d in range(1, 6)])
    eff = coco_efficiency(econ, daily, cap_credits=15.0).set_index("USER_NAME")
    b, a = eff.loc["B"], eff.loc["A"]
    for col in ("CACHE_HIT_PCT", "READ_AMP", "CACHE_WRITE_PCT", "TOTAL"):
        assert pd.isna(b[col]), col
    assert a["CACHE_HIT_PCT"] > 98 and a["READ_AMP"] > 0          # a measured user keeps its numbers
    assert b["TOTAL_CREDITS"] == 40.0 and b["ACTIVE_DAYS"] == 5     # the credit signals are still measured
    assert eff.index[0] == "B"                                      # still sorted by credits (NaN-safe)
    assert coco_coaching_count(eff.reset_index()) == 0


def test_no_prompt_tokens_is_no_hit_rate_and_the_fleet_rate_ignores_it():
    econ = token_economics(pd.DataFrame(_tokens("OUT_ONLY", 0, 500, 0, 0) + _tokens("C", 100, 10, 300, 0)))
    e = econ.set_index("USER_NAME")
    assert pd.isna(e.loc["OUT_ONLY", "CACHE_HIT_PCT"]) and e.loc["C", "CACHE_HIT_PCT"] == 75.0
    assert fleet_cache_hit_pct(econ) == 75.0
    assert int((econ["CACHE_HIT_PCT"] < 80).sum()) == 1             # the panel's low-cache test skips the NaN
