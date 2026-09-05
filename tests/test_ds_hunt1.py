"""Decision Studio bug-hunt #1 locks (2026-08-30, v4.365.0).

Adversarial DS pass (6 finders). Six confirmed (findings #5/#6 are the same slo_summary bug found by
two finders), zero refuted. All app-side, no migration.
  - [MED] proof_verdict dropped the untagged-share caveat and left level='good', headlining the
    untrustworthy precision as proof.
  - [MED] slo_summary computed worst_burn/has_burn over ALL rows, so a STALE objective (verdict
    withheld) still fired the reliability alarm.
  - [MED] _products "Consumers served" summed per-product distinct readers (overcounts distinct
    accounts) -> relabeled to an honest "Consumer reach".
  - [LOW] _scenarios rendered "$0.00" for eligible-but-unpriced candidates -> "Unpriced".
  - [LOW] _scenarios silently truncated the projection at the action_center 500-row cap.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.decision import scenario_projection, slo_summary
from app.logic.proof import proof_verdict

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Finding #3 -- proof_verdict surfaces the untrustworthy-precision caveat
# --------------------------------------------------------------------------- #
def test_untrustworthy_precision_downgrades_verdict_to_watch() -> None:
    roi = {"RATIO": 2.0, "PAYS": True}
    precision = {"PRECISION_PCT": 90.0, "UNTAGGED_SHARE_PCT": 55.0}  # 55% > 40% floor
    verdict = proof_verdict(roi, realization_pct=80.0, acceptance_pct=70.0, precision=precision)
    assert verdict["level"] == "watch", "high untagged share must downgrade the verdict"
    # the caveat reaches the flagship headline, and the green 'earning its keep' claim does not
    assert "unlabeled" in verdict["headline"]
    assert "earning its keep" not in verdict["headline"]
    assert any("unlabeled" in r for r in verdict["reasons"])


def test_trustworthy_precision_stays_good() -> None:
    roi = {"RATIO": 2.0, "PAYS": True}
    precision = {"PRECISION_PCT": 90.0, "UNTAGGED_SHARE_PCT": 10.0}  # below the 40% floor
    verdict = proof_verdict(roi, realization_pct=80.0, acceptance_pct=70.0, precision=precision)
    assert verdict["level"] == "good"
    assert "earning its keep" in verdict["headline"]


# --------------------------------------------------------------------------- #
# Findings #5/#6 -- worst_burn / has_burn respect the STALE verdict withholding
# --------------------------------------------------------------------------- #
def test_worst_burn_excludes_stale_objectives() -> None:
    frame = pd.DataFrame({
        "STATUS": ["MET", "BREACH", "STALE"],
        "BURN_MULTIPLE": [0.5, 1.5, 9.0],   # the stale 9.0x must NOT drive the alarm
    })
    s = slo_summary(frame)
    assert s["worst_burn"] == 1.5, "stale objective's burn must not become the worst burn"
    assert s["has_burn"] == 1.0
    assert s["stale"] == 1.0 and s["breach"] == 1.0


def test_all_stale_burn_reads_as_no_burn() -> None:
    frame = pd.DataFrame({"STATUS": ["STALE", "NO_DATA"], "BURN_MULTIPLE": [4.0, None]})
    s = slo_summary(frame)
    assert s["worst_burn"] == 0.0
    assert s["has_burn"] == 0.0, "a burn carried only by a verdict-withheld row must not alarm"


def test_fresh_breach_still_drives_the_burn_alarm() -> None:
    frame = pd.DataFrame({"STATUS": ["BREACH"], "BURN_MULTIPLE": [3.0]})
    s = slo_summary(frame)
    assert s["worst_burn"] == 3.0 and s["has_burn"] == 1.0


# --------------------------------------------------------------------------- #
# Finding #1 -- scenario_projection can produce eligible-but-unpriced candidates
# --------------------------------------------------------------------------- #
def test_unpriced_candidates_are_counted_but_gross_is_zero() -> None:
    actions = pd.DataFrame({
        "STATUS": ["OPEN", "OPEN"],
        "CONFIDENCE": [0.7, 0.8],
        "ESTIMATED_USD": [None, None],          # security-style actions carry no dollar estimate
        "SOURCE_ENTITY_TYPE": ["ROLE", "ROLE"],
        "SOURCE_ENTITY_KEY": ["R1", "R2"],
        "ACTION_ID": ["a1", "a2"],
    })
    proj = scenario_projection(actions, adoption_pct=60, realization_pct=70, confidence_floor=0.6)
    assert proj["candidates"] == 2.0            # both are eligible
    assert proj["gross_estimate"] == 0.0        # but unpriced -> the UI must show "Unpriced", not $0
    assert proj["expected_capture"] == 0.0


def test_scenarios_ui_distinguishes_unpriced_and_discloses_the_cap() -> None:
    src = _src("app/ui/decision_studio.py")
    # unpriced vs measured-zero
    assert '_priced = has_candidates and projection["gross_estimate"] > 0' in src
    assert 'return format_usd(value) if _priced else "Unpriced"' in src
    # 500-cap disclosure
    assert "if len(actions.df) >= 500:" in src
    assert "Projecting the top 500 open actions" in src


# --------------------------------------------------------------------------- #
# Finding #4 -- the product-consumer KPI is labeled as reach, not distinct accounts
# --------------------------------------------------------------------------- #
def test_consumer_reach_kpi_is_honestly_labeled() -> None:
    src = _src("app/ui/decision_studio.py")
    # deferred-item: the aggregate consumer metrics folded from a KPI card into a
    # caption; the honest "reach" framing (not "distinct accounts") lives there now.
    assert "Consumer reach" in src
    assert "sum of each product's distinct readers" in src   # honest reach framing
    assert '_reach = int(safe_float(verdicts.get("DISTINCT_CONSUMERS"' in src
    # the old false "distinct accounts" claim and the old label are gone
    assert '"label": "Consumers served"' not in src
    assert "Distinct accounts that read these products' objects" not in src
