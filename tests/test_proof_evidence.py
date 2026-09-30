"""v4.597 (Decision Studio Option C, slice S4): the Proof data + logic layer (no UI yet).

Locks:
  * mart_sql.savings_ledger gains ADDITIVE evidence projections only (every pre-existing alias and
    the V153 re-measure expression unchanged — tests/test_ledger_twins.py keeps its own locks), incl.
    SOURCE_CHANGE_ID, which fixes the always-0 "auto-measured" count in actions.ledger_totals;
  * mart_sql.ledger_attribution: parses, the ATTRIBUTION CASE order, STARTSWITH/CONTAINS (never LIKE)
    for the warehouse match, uncapped window totals anchored exactly like savings_summary_quarter,
    its own cache domain (alerts) kept OFF savings_ledger, and a canary right after savings_ledger;
  * proof.evidence_rows / evidence_split / carried_realization;
  * savings_rollup.idle_opportunities / resize_opportunities are byte-equal to the inline generators
    they replaced in Cost ▸ Optimize, which now calls them;
  * decision.monthly_equivalent / pipeline_frame.
"""

from __future__ import annotations

import re
import warnings
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.config import LEDGER_TWIN_MATCH_DAYS, SAVINGS_ACTIVE_MONTHS
from app.data import mart_sql
from app.data.common import account_today_sql
from app.logic import proof
from app.logic.actions import ledger_totals
from app.logic.decision import monthly_equivalent, pipeline_frame, scenario_projection
from app.logic.formulas import safe_float
from app.logic.savings_rollup import (
    SavingsOpportunity,
    confidence_weight,
    idle_opportunities,
    resize_opportunities,
    rollup_savings,
)

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# mart_sql.savings_ledger — additive evidence projections
# ---------------------------------------------------------------------------
_NEW_LEDGER_PROJECTIONS = (
    "l.SOURCE_CHANGE_ID,", "l.TARGET_OBJECT,",
    "r.WAREHOUSE_NAME AS CHANGE_WAREHOUSE", "r.SETTING AS CHANGE_SETTING",
    "r.OLD_VALUE AS CHANGE_OLD_VALUE", "r.NEW_VALUE AS CHANGE_NEW_VALUE",
    "r.CHANGE_SEEN_AT,", "r.VERDICT AS CHANGE_VERDICT", "r.TRACKING_UNTIL,", "r.AFTER_QUERIES,",
    "r.CHANGED_BY AS CHANGE_BY",
    f"IFF(r.CHANGE_ID IS NULL, NULL, {account_today_sql()} > r.TRACKING_UNTIL) AS WINDOW_CLOSED",
)
_PRE_V4597_LEDGER_COLUMNS = [
    "ITEM_ID", "ACTION_ID", "CREATED_AT", "DESCRIPTION", "STATE", "ESTIMATED_USD", "VERIFIED_USD",
    "VERIFIED_AT", "VERIFIED_BY", "PROOF_SQL", "NOTES", "FINDING_TYPE", "SOURCE",
    "SUPERSEDED_BY_CHANGE_ID", "MEASURED_AFTER_DAYS", "REMEASURED_14D_MONTHLY_USD", "VOLUME_RATIO",
    "VOLUME_CONFOUNDED",
]


def test_savings_ledger_projections_are_additive_only():
    sqlglot = pytest.importorskip("sqlglot")
    for limit in (None, 500):
        sql = mart_sql.savings_ledger(limit=limit)
        for fragment in _NEW_LEDGER_PROJECTIONS:
            assert fragment in sql, fragment
        cols = sqlglot.parse_one(sql, read="snowflake").named_selects
        # every pre-existing column keeps its name AND its position; the new ones only append
        assert cols[:len(_PRE_V4597_LEDGER_COLUMNS)] == _PRE_V4597_LEDGER_COLUMNS
        assert cols[len(_PRE_V4597_LEDGER_COLUMNS):] == [
            "SOURCE_CHANGE_ID", "TARGET_OBJECT", "CHANGE_WAREHOUSE", "CHANGE_SETTING", "CHANGE_OLD_VALUE",
            "CHANGE_NEW_VALUE", "CHANGE_SEEN_AT", "CHANGE_VERDICT", "TRACKING_UNTIL", "AFTER_QUERIES",
            "CHANGE_BY", "WINDOW_CLOSED",
            # Next-Fifty #31 (v4.600): the revert projections, appended after WINDOW_CLOSED
            "REVERTED_AT", "REVERT_CHANGE_ID", "REVERT_OLD_VALUE", "REVERT_NEW_VALUE", "REVERT_KIND",
            # review r1 F1: the setting the undoing change touched (a co-attributed partner's when inherited)
            "REVERT_SETTING"]
        # registry + twin + the #31 revert link (a registry-only CTE); no alerts domain
        assert sql.count("LEFT JOIN") == 3 and "ALERT_EVENTS" not in sql and "REMEDIATION_LOG" not in sql
        assert "LEFT JOIN rv ON rv.BOOKED_CHANGE_ID = l.SOURCE_CHANGE_ID" in sql
        assert "CURRENT_DATE()" not in sql        # the app clock (TZ standard), never session-tz
    assert "LIMIT" not in mart_sql.savings_ledger(limit=None)


def test_savings_ledger_new_columns_do_not_leak_into_the_optimize_display():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    shown = opt.split("styled_table(_ledger_view[[c for c in (", 1)[1].split(")", 1)[0]
    for col in ("SOURCE_CHANGE_ID", "CHANGE_BY", "TRACKING_UNTIL", "WINDOW_CLOSED", "CHANGE_SEEN_AT"):
        assert col not in shown, col


# ---------------------------------------------------------------------------
# actions.ledger_totals — the auto-measured count live defect
# ---------------------------------------------------------------------------
def test_auto_measured_count_reads_the_real_frame_shape():
    # The REAL pre-fix frame: savings_ledger projected only the derived SOURCE, never SOURCE_CHANGE_ID,
    # so verified_no_estimate_auto_count was always 0 ("(0 auto-measured, N verified by hand)").
    legacy = pd.DataFrame({
        "STATE": ["VERIFIED"] * 3,
        "ESTIMATED_USD": [0.0, 0.0, None],
        "VERIFIED_USD": [100.0, 50.0, 25.0],
        "SOURCE": ["auto", "auto", "manual"],
    })
    t = ledger_totals(legacy)
    assert (t["verified_no_estimate_count"], t["verified_no_estimate_auto_count"]) == (3, 2)
    # the post-fix frame carries both columns (they agree by construction in SQL)
    both = legacy.assign(SOURCE_CHANGE_ID=["C1", "C2", None])
    assert ledger_totals(both)["verified_no_estimate_auto_count"] == 2
    # the id alone still counts (a frame without SOURCE, e.g. tests/test_ds_roi_fixes.py)
    ids_only = legacy.drop(columns="SOURCE").assign(SOURCE_CHANGE_ID=["C1", " ", None])
    assert ledger_totals(ids_only)["verified_no_estimate_auto_count"] == 1
    # an estimated row never enters the verified split
    est = pd.DataFrame({"STATE": ["ESTIMATED"], "ESTIMATED_USD": [0.0], "VERIFIED_USD": [None],
                        "SOURCE": ["auto"]})
    assert ledger_totals(est)["verified_no_estimate_auto_count"] == 0


def test_ledger_totals_fix_is_wired_to_the_builder_column():
    # the builder now projects the column ledger_totals reads first
    assert "l.SOURCE_CHANGE_ID," in mart_sql.savings_ledger(limit=None)
    src = _src("app/logic/actions.py")
    body = src.split("def ledger_totals(", 1)[1].split("\ndef ", 1)[0]
    assert '_auto = _auto | (ver["SOURCE"].astype(str).str.strip().str.lower() == "auto")' in body


# ---------------------------------------------------------------------------
# mart_sql.ledger_attribution
# ---------------------------------------------------------------------------
def _attribution_case_order(sql: str) -> list[str]:
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    tree = sqlglot.parse_one(sql, read="snowflake")
    for alias in tree.find_all(exp.Alias):
        if alias.alias == "ATTRIBUTION" and isinstance(alias.this, exp.Case):
            case = alias.this
            thens = [str(if_.args["true"].this) for if_ in case.args["ifs"]]
            return [*thens, str(case.args["default"].this)]
    raise AssertionError("no ATTRIBUTION CASE")


def test_ledger_attribution_parses_and_orders_its_classes():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.ledger_attribution()
    sqlglot.parse_one(sql, read="snowflake")
    assert _attribution_case_order(sql) == list(mart_sql.LEDGER_ATTRIBUTION_ORDER) == [
        "EXPERIMENT", "OVERWATCH_BOOKED", "OVERWATCH_EXECUTED", "OVERWATCH_RECOMMENDED", "DETECTED_ELSEWHERE"]
    case = sql.split("AS ATTRIBUTION", 1)[0].rsplit("CASE WHEN", 1)[1]
    executed = case.split("THEN 'OVERWATCH_BOOKED'", 1)[1].split("THEN 'OVERWATCH_EXECUTED'", 1)[0]
    # executed = adopted note, alert closed-loop note, a remediation match, or a superseded twin
    for signal in ("'adopted by the daily change scan'", "'From alert event '",
                   "rem.REMEDIATION_ID IS NOT NULL", "te.ITEM_ID IS NOT NULL"):
        assert signal in executed, signal
    assert "rec.REC_EVENT_ID IS NOT NULL THEN 'OVERWATCH_RECOMMENDED'" in case
    # CHANGED_BY is too weak to classify on (nearly always NULL)
    assert "CHANGED_BY" not in sql


def test_ledger_attribution_matches_remediations_without_like_wildcards():
    sql = mart_sql.ledger_attribution()
    rem = sql.split("rem AS (", 1)[1].split("\n),", 1)[0]
    assert " LIKE " not in rem.upper()                   # '_' in warehouse names is a LIKE wildcard
    assert "STARTSWITH(UPPER(TRIM(rl.STATEMENT_SQL)), 'ALTER WAREHOUSE ' || UPPER(r.WAREHOUSE_NAME) || ' ')" in rem
    assert "CONTAINS(UPPER(rl.STATEMENT_SQL)" in rem
    for token in ("'WAREHOUSE_SIZE'", "'MAX_CLUSTER_COUNT'", "'MIN_CLUSTER_COUNT'", "UPPER(r.SETTING)"):
        assert token in rem, token
    assert "UPPER(rl.STATUS) = 'EXECUTED'" in rem
    assert f"rl.EXECUTED_AT > DATEADD('day', -{int(LEDGER_TWIN_MATCH_DAYS)}, r.CHANGE_SEEN_AT::TIMESTAMP_NTZ)" in rem
    assert "rl.EXECUTED_AT <= DATEADD('hour', 1, r.CHANGE_SEEN_AT::TIMESTAMP_NTZ)" in rem
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY r.CHANGE_ID ORDER BY rl.EXECUTED_AT DESC) = 1" in rem
    rec = sql.split("rec AS (", 1)[1].split("\n),", 1)[0]
    assert "e.RULE_ID = 'COST_IDLE_OPPORTUNITY'" in rec and "r.SETTING = 'AUTO_SUSPEND'" in rec
    assert "SPLIT_PART(e.DEDUPE_KEY, '|', 2) = UPPER(r.WAREHOUSE_NAME)" in rec
    assert "e.RAISED_AT <= r.CHANGE_SEEN_AT::TIMESTAMP_NTZ" in rec
    assert "e.RAISED_AT >= DATEADD('day', -30, r.CHANGE_SEEN_AT::TIMESTAMP_NTZ)" in rec
    assert " LIKE " not in rec.upper()


def test_ledger_attribution_window_totals_are_uncapped_and_quarter_anchored():
    sql = mart_sql.ledger_attribution()
    # the same active anchor as savings_summary_quarter (the ROI numerator), twin-excluded
    anchor = f"DATEADD('month', -{int(SAVINGS_ACTIVE_MONTHS)}, {account_today_sql()})"
    assert anchor in mart_sql.savings_summary_quarter()
    assert f"l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL AND l.VERIFIED_AT >= {anchor}" in sql
    for window in (
        "ROUND(SUM(a.ACTIVE_ROW_USD) OVER (PARTITION BY a.ATTRIBUTION), 2) AS ATTR_ACTIVE_USD",
        "ROUND(SUM(a.ACTIVE_ROW_USD) OVER (PARTITION BY a.CHANGE_VERDICT), 2) AS VERDICT_ACTIVE_USD",
        "ROUND(SUM(a.ACTIVE_ROW_USD) OVER (), 2) AS ACTIVE_USD",
        "ROUND(SUM(IFF(a.SHORT_WINDOW_N = 1, a.ACTIVE_ROW_USD, 0)) OVER (), 2) AS SHORT_WINDOW_ACTIVE_USD",
        "COUNT(*) OVER () AS TOTAL_ITEMS",
    ):
        assert window in sql, window
    # the per-class split is pivoted onto EVERY row (whole-ledger even if a class falls off a capped frame)
    for alias in ("EXECUTED", "RECOMMENDED", "BOOKED", "ELSEWHERE", "EXPERIMENT", "REGRESSED", "NEUTRAL"):
        assert f") OVER (), 2) AS {alias}_ACTIVE_USD" in sql, alias
    # short window = an auto row whose note lacks the V153 'full window' settle wording
    assert ("IFF(l.SOURCE_CHANGE_ID IS NOT NULL\n               AND NOT CONTAINS(COALESCE(l.NOTES, ''), 'full window')\n"
            "               AND NOT CONTAINS(COALESCE(l.NOTES, ''), 're-settled on the full 14-day window'), 1, 0)") in sql
    # the sentinel is the one resettle_autobook_14d.sql grid 2 appends (review r1)
    resettle = (_ROOT / "snowflake" / "resettle_autobook_14d.sql").read_text(encoding="utf-8")
    assert "re-settled on the full 14-day window" in resettle
    assert "LIMIT" not in sql and "ORDER BY a.CREATED_AT DESC" in sql    # same newest-first order as the ledger
    assert "ACTION_QUEUE" not in sql and "CURRENT_DATE()" not in sql


def test_ledger_attribution_owns_the_alerts_domain_and_the_ledger_does_not():
    from app.core.query import _domains_in
    attr = set(_domains_in(mart_sql.ledger_attribution()))
    assert {"alerts", "ledger", "remediation"} <= attr
    ledger = set(_domains_in(mart_sql.savings_ledger(limit=None)))
    assert "alerts" not in ledger and "remediation" not in ledger and "ledger" in ledger


def test_ledger_attribution_has_a_canary_right_after_the_ledger():
    canary = _src("app/data/canary.py")
    assert ('("mart.savings_ledger", mart_sql.savings_ledger),\n'
            '    ("mart.ledger_attribution", mart_sql.ledger_attribution),') in canary


# ---------------------------------------------------------------------------
# proof.evidence_rows / evidence_split / carried_realization
# ---------------------------------------------------------------------------
_TODAY = date(2026, 9, 27)


def _ledger() -> pd.DataFrame:
    return pd.DataFrame([
        # V153 full-window settle, volume-confounded, executed by OVERWATCH (LTZ seen-at, NTZ verified-at)
        {"ITEM_ID": "a", "STATE": "VERIFIED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": 100.0,
         "FINDING_TYPE": "AUTO_SUSPEND", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C1", "TARGET_OBJECT": None,
         "CHANGE_WAREHOUSE": "WH_A", "CHANGE_OLD_VALUE": "600", "CHANGE_NEW_VALUE": "60",
         "CHANGE_VERDICT": "IMPROVED", "MEASURED_AFTER_DAYS": 14.0, "TRACKING_UNTIL": date(2026, 9, 10),
         "NOTES": "Auto-booked | measured on the full window: 2 credits/day -> 1 (IMPROVED); floor $5/mo.",
         "CHANGE_SEEN_AT": pd.Timestamp("2026-08-27 06:40", tz="America/Chicago"),
         "VERIFIED_AT": pd.Timestamp("2026-09-11 06:45"), "VOLUME_CONFOUNDED": True},
        # still measuring: settles the morning after TRACKING_UNTIL
        {"ITEM_ID": "b", "STATE": "ESTIMATED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": None,
         "FINDING_TYPE": "RESIZE", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C2", "TARGET_OBJECT": "",
         "CHANGE_WAREHOUSE": "WH_B", "CHANGE_OLD_VALUE": "LARGE", "CHANGE_NEW_VALUE": "SMALL",
         "CHANGE_VERDICT": "PENDING", "MEASURED_AFTER_DAYS": 3.0, "TRACKING_UNTIL": date(2026, 10, 5),
         "NOTES": "Auto-booked from the daily warehouse-change scan", "VERIFIED_AT": None},
        # app-booked, hand-verified: no measured window
        {"ITEM_ID": "c", "STATE": "VERIFIED", "ESTIMATED_USD": 50.0, "VERIFIED_USD": 40.0,
         "FINDING_TYPE": "SCHEDULE", "SOURCE": "manual", "SOURCE_CHANGE_ID": None, "TARGET_OBJECT": "WH_C",
         "NOTES": "booked from Optimize", "VERIFIED_AT": pd.Timestamp("2026-08-01 10:00")},
        # pre-V153 short-window settle, REGRESSED yet saved, LBA-1 co-attributed $0
        {"ITEM_ID": "d", "STATE": "VERIFIED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": 0.0,
         "FINDING_TYPE": "MAX_CLUSTERS", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C4",
         "CHANGE_WAREHOUSE": "WH_D", "CHANGE_OLD_VALUE": None, "CHANGE_NEW_VALUE": "2",
         "CHANGE_VERDICT": "REGRESSED",
         "NOTES": "Auto | measured 3 -> 1 credits/day over 3d (REGRESSED) | LBA-1 co-attributed: once on C9.",
         "VERIFIED_AT": pd.Timestamp("2026-07-01 07:00")},
        # closed with nothing metered after the change
        {"ITEM_ID": "e", "STATE": "REJECTED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": None,
         "FINDING_TYPE": "AUTO_SUSPEND", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C5", "CHANGE_WAREHOUSE": "WH_E",
         "CHANGE_VERDICT": "NO_BASELINE",
         "NOTES": "Auto | not measurable (NO_BASELINE): no metered credits after the change",
         "VERIFIED_AT": pd.Timestamp("2026-09-01 07:00")},
        # superseded manual twin: excluded from the evidence rows like from every total
        {"ITEM_ID": "f", "STATE": "ESTIMATED", "ESTIMATED_USD": 75.0, "VERIFIED_USD": None,
         "FINDING_TYPE": "AUTO_SUSPEND", "SOURCE": "manual", "SOURCE_CHANGE_ID": None, "TARGET_OBJECT": "WH_A",
         "SUPERSEDED_BY_CHANGE_ID": "C1", "NOTES": "manual"},
    ])


def _attribution(**pivot) -> pd.DataFrame:
    totals = {"ACTIVE_USD": 140.0, "EXECUTED_ACTIVE_USD": 100.0, "RECOMMENDED_ACTIVE_USD": 0.0,
              "BOOKED_ACTIVE_USD": 40.0, "ELSEWHERE_ACTIVE_USD": 0.0, "EXPERIMENT_ACTIVE_USD": 0.0,
              "REGRESSED_ACTIVE_USD": 0.0, "NEUTRAL_ACTIVE_USD": 0.0, "SHORT_WINDOW_ACTIVE_USD": 0.0,
              "ACTIVE_ITEMS": 3, "TOTAL_ITEMS": 6, **pivot}
    rows = [
        {"ITEM_ID": "a", "ATTRIBUTION": "OVERWATCH_EXECUTED", "CHANGE_VERDICT": "IGNORED",
         "TWIN_ESTIMATED_USD": 75.0, "REMEDIATION_EST_USD": 120.0, "REC_EST_USD": None, "ATTR_ACTIVE_USD": 100.0},
        {"ITEM_ID": "b", "ATTRIBUTION": "DETECTED_ELSEWHERE", "ATTR_ACTIVE_USD": 0.0},
        {"ITEM_ID": "c", "ATTRIBUTION": "OVERWATCH_BOOKED", "ATTR_ACTIVE_USD": 40.0},
        {"ITEM_ID": "d", "ATTRIBUTION": "OVERWATCH_RECOMMENDED", "REC_EST_USD": 30.0, "ATTR_ACTIVE_USD": 0.0},
        {"ITEM_ID": "e", "ATTRIBUTION": "OVERWATCH_EXECUTED", "REMEDIATION_EST_USD": 20.0,
         "ATTR_ACTIVE_USD": 100.0},
        {"ITEM_ID": "f", "ATTRIBUTION": "OVERWATCH_BOOKED", "ATTR_ACTIVE_USD": 40.0},
    ]
    return pd.DataFrame([{**r, **totals} for r in rows])


def test_evidence_rows_grade_each_saving():
    ev = proof.evidence_rows(_ledger(), _attribution(), _TODAY)
    assert list(ev.columns) == list(proof.EVIDENCE_COLUMNS)
    assert len(ev) == 5                                          # the superseded twin is excluded
    by = {r.LEVER + ":" + str(r.TARGET): r for r in ev.itertuples()}
    a = by["AUTO_SUSPEND:WH_A"]
    assert (a.STATE, a.VERIFIED_USD, a.CHANGE, a.VERDICT) == ("VERIFIED", 100.0, "600 → 60", "IMPROVED")
    assert a.WINDOW == "full 14-day window" and a.ATTRIBUTION == "Executed by OVERWATCH"
    assert a.FLAGS == "volume-confounded" and a.MEASURED_AFTER_DAYS == 14.0
    b = by["RESIZE:WH_B"]                                        # TARGET falls back to the change's warehouse
    assert b.WINDOW == "settles ~Oct 6" and b.CHANGE == "LARGE → SMALL" and pd.isna(b.VERIFIED_USD)
    assert b.ATTRIBUTION == "Detected elsewhere" and b.FLAGS is None
    c = by["SCHEDULE:WH_C"]                                      # app-booked: no window, no change
    assert c.WINDOW is None and c.CHANGE is None and c.VERDICT is None and c.ATTRIBUTION == "Booked in OVERWATCH"
    d = by["MAX_CLUSTERS:WH_D"]
    assert d.WINDOW == "short window (pre-V153)" and d.CHANGE == "? → 2"
    assert d.FLAGS == "cheaper but slower · co-attributed $0"
    assert d.ATTRIBUTION == "Recommended by OVERWATCH, executed elsewhere"
    e = by["AUTO_SUSPEND:WH_E"]
    assert e.WINDOW == "not measurable" and e.FLAGS == "performance unjudged"
    # ledger values win over same-named attribution columns (CHANGE_VERDICT)
    assert "IGNORED" not in set(ev["VERDICT"].dropna())
    # sorted by VERIFIED_USD desc, NULL last (renders "—" through the shared grid placeholder)
    assert list(ev["VERIFIED_USD"].head(3)) == [100.0, 40.0, 0.0] and ev["VERIFIED_USD"].tail(2).isna().all()
    assert ev["VERIFIED_AT"].dt.tz is None
    assert ev.loc[ev["TARGET"] == "WH_C", "VERIFIED_AT"].iloc[0] == pd.Timestamp("2026-08-01 10:00")


def test_evidence_rows_normalise_ltz_timestamps_to_naive_account_time():
    ledger = _ledger()
    # a tz-aware (LTZ-style) VERIFIED_AT beside naive NTZ ones: element-wise normalisation keeps both
    # (a vectorised to_datetime over the mix silently NaT-s one kind), in account (Central) wall time
    ledger["VERIFIED_AT"] = ledger["VERIFIED_AT"].astype("object")
    ledger.loc[0, "VERIFIED_AT"] = pd.Timestamp("2026-09-11 11:45", tz="UTC")
    ev = proof.evidence_rows(ledger, None, _TODAY)
    assert ev["VERIFIED_AT"].dt.tz is None
    assert ev.loc[ev["TARGET"] == "WH_A", "VERIFIED_AT"].iloc[0] == pd.Timestamp("2026-09-11 06:45")
    assert ev.loc[ev["TARGET"] == "WH_C", "VERIFIED_AT"].iloc[0] == pd.Timestamp("2026-08-01 10:00")
    # a tz-aware TRACKING_UNTIL-style clock still drives the settle date
    ledger.loc[1, "TRACKING_UNTIL"] = pd.Timestamp("2026-10-05", tz="America/Chicago")
    assert proof.evidence_rows(ledger, None, _TODAY).loc[
        lambda f: f["TARGET"] == "WH_B", "WINDOW"].iloc[0] == "settles ~Oct 6"


def test_evidence_rows_settle_clock_and_degradation():
    ledger = _ledger()
    # a closed window the scan has not settled yet
    late = proof.evidence_rows(ledger, None, date(2026, 10, 20))
    assert late.loc[late["TARGET"] == "WH_B", "WINDOW"].iloc[0] == "awaiting settle (window closed Oct 5)"
    # settle date == today still reads as settling today
    assert proof.evidence_rows(ledger, None, date(2026, 10, 6)).loc[
        lambda f: f["TARGET"] == "WH_B", "WINDOW"].iloc[0] == "settles ~Oct 6"
    # no attribution read -> labels are NULL ("—"), never guessed
    assert late["ATTRIBUTION"].isna().all()
    # an older ledger read without SOURCE_CHANGE_ID still recognises auto rows from SOURCE
    legacy = ledger.drop(columns=["SOURCE_CHANGE_ID"])
    assert proof.evidence_rows(legacy, None, _TODAY).loc[
        lambda f: f["TARGET"] == "WH_A", "WINDOW"].iloc[0] == "full 14-day window"
    empty = proof.evidence_rows(pd.DataFrame(), None, _TODAY)
    assert empty.empty and list(empty.columns) == list(proof.EVIDENCE_COLUMNS)
    assert proof.evidence_rows(None, None, _TODAY).empty
    # a shaped-harness style frame (floats where dates/strings are expected) never raises
    shaped = pd.DataFrame({c: [1.0, 2.0] for c in ("ITEM_ID", "VERIFIED_USD", "TRACKING_UNTIL",
                                                     "WINDOW_CLOSED", "CHANGE_BY")})
    shaped["STATE"] = ["STATE_0", "STATE_1"]
    shaped["SOURCE_CHANGE_ID"] = ["SOURCE_CHANGE_ID_0", "SOURCE_CHANGE_ID_1"]
    shaped["CHANGE_SEEN_AT"] = pd.Timestamp("2026-08-15")
    assert len(proof.evidence_rows(shaped, None, _TODAY)) == 2


def test_evidence_split_reads_sql_window_columns_never_sums():
    att = _attribution()
    split = proof.evidence_split(att)
    # every row carries the same pivoted totals; a pandas SUM would read 6x these
    assert split["active_usd"] == 140.0 and split["executed_usd"] == 100.0 and split["booked_usd"] == 40.0
    assert (split["recommended_usd"], split["elsewhere_usd"], split["experiment_usd"]) == (0.0, 0.0, 0.0)
    assert (split["regressed_usd"], split["neutral_usd"], split["short_window_usd"]) == (0.0, 0.0, 0.0)
    assert (split["active_items"], split["total_items"], split["capped"]) == (3, 6, False)
    # a row-capped frame (fewer rows than TOTAL_ITEMS) keeps the whole-ledger totals and says so
    capped = proof.evidence_split(att.head(2))
    assert capped["active_usd"] == 140.0 and capped["capped"] is True
    assert proof.evidence_split(None) == {} and proof.evidence_split(pd.DataFrame()) == {}
    assert proof.evidence_split(att.drop(columns=["EXECUTED_ACTIVE_USD"])) == {}
    src = _src("app/logic/proof.py").split("def evidence_split(", 1)[1].split("\ndef ", 1)[0]
    assert ".sum(" not in src


def test_carried_realization_uses_the_estimate_recorded_before_the_change():
    rows = proof.ledger_with_attribution(_ledger(), _attribution())
    carried = proof.carried_realization(rows)
    assert carried is not None
    # a: own estimate 0 -> the superseded twin's 75 wins over the remediation's 120 (first positive)
    # c: own estimate 50, realized 40
    # d: LBA-1 co-attributed $0 -> excluded even though it carries a recommendation estimate
    # e: REJECTED with a carried remediation estimate -> counts as 0 realized (a real miss)
    # b: still ESTIMATED -> not judged yet; f: superseded twin -> dropped
    assert carried["estimated_usd"] == 75.0 + 50.0 + 20.0
    assert carried["realized_usd"] == 100.0 + 40.0 + 0.0
    assert carried["carried_pct"] == round(140.0 / 145.0 * 100, 1)
    assert (carried["items"], carried["rejected_items"], carried["carried_items"]) == (3, 1, 2)
    assert carried["by_source"] == {"own": 1, "twin": 1, "remediation": 1, "recommendation": 0}
    # a SEPARATE figure: ledger_totals' realization_pct is untouched (only c has its own estimate)
    assert ledger_totals(rows)["realization_pct"] == 80.0
    # nothing eligible -> None (never a fabricated 0%)
    assert proof.carried_realization(_ledger()[lambda f: f["ITEM_ID"] == "b"]) is None
    assert proof.carried_realization(None) is None and proof.carried_realization(pd.DataFrame()) is None


def test_ledger_with_attribution_keeps_the_ledger_when_the_read_fails():
    ledger = _ledger()
    for bad in (None, pd.DataFrame(), pd.DataFrame({"X": [1]})):
        out = proof.ledger_with_attribution(ledger, bad)
        assert list(out.columns) == list(ledger.columns) and len(out) == len(ledger)
    merged = proof.ledger_with_attribution(ledger, _attribution())
    assert list(merged["ITEM_ID"]) == list(ledger["ITEM_ID"])              # ledger order kept
    assert merged.loc[0, "CHANGE_VERDICT"] == "IMPROVED"                   # ledger value wins


# ---------------------------------------------------------------------------
# savings_rollup — extracted opportunity generators (byte-equal)
# ---------------------------------------------------------------------------
def _old_idle(advisor: pd.DataFrame) -> list[SavingsOpportunity]:
    # verbatim copy of the pre-v4.597 inline generator in cost_parts/optimize.py
    return [
        SavingsOpportunity("IDLE", str(r["WAREHOUSE_NAME"]),
                           safe_float(r["ACTIONABLE_MONTHLY_USD"]),
                           confidence_weight(r.get("SAVINGS_CONFIDENCE")))
        for _, r in advisor.iterrows()
        if safe_float(r["ACTIONABLE_MONTHLY_USD"]) > 0]


def _old_resize(sized: pd.DataFrame) -> list[SavingsOpportunity]:
    return [
        SavingsOpportunity("RESIZE", str(r["WAREHOUSE_NAME"]),
                           safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")),
                           confidence_weight(r.get("CONFIDENCE")))
        for _, r in sized.iterrows()
        if safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")) > 0]


def test_opportunity_helpers_are_byte_equal_to_the_inline_generators():
    advisor = pd.DataFrame({
        "WAREHOUSE_NAME": ["WH_A", "WH_B", "WH_C", "WH_D", "WH_E"],
        "ACTIONABLE_MONTHLY_USD": [120.5, 0.0, None, 33.3, "7"],
        "SAVINGS_CONFIDENCE": ["MEDIUM", "LOW", "MEDIUM", None, "HIGH"],
    })
    sized = pd.DataFrame({
        "WAREHOUSE_NAME": ["WH_A", "WH_X", "WH_Y"],
        "POTENTIAL_MONTHLY_SAVING_USD": [200.0, -5.0, 12.0],
        "CONFIDENCE": ["LOW", "MEDIUM", "bogus"],
    })
    assert idle_opportunities(advisor) == _old_idle(advisor)
    assert resize_opportunities(sized) == _old_resize(sized)
    assert [o.target for o in idle_opportunities(advisor)] == ["WH_A", "WH_D", "WH_E"]
    # the rollup over the helper output is identical too
    assert (rollup_savings(idle_opportunities(advisor) + resize_opportunities(sized))
            == rollup_savings(_old_idle(advisor) + _old_resize(sized)))
    # a sizing frame without the saving column yields nothing, like the old r.get() path
    assert resize_opportunities(sized.drop(columns=["POTENTIAL_MONTHLY_SAVING_USD"])) == []
    for empty in (None, pd.DataFrame()):
        assert idle_opportunities(empty) == [] and resize_opportunities(empty) == []


def test_optimize_calls_the_shared_helpers():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert opt.count("_savings_opps.extend(idle_opportunities(advisor))") == 1
    assert opt.count("_savings_opps.extend(resize_opportunities(sized))") == 1
    assert 'SavingsOpportunity("IDLE"' not in opt and 'SavingsOpportunity("RESIZE"' not in opt
    assert opt.count("ACCOUNT_USAGE") == 5                    # tests/test_perf_budgets.py ceiling


# ---------------------------------------------------------------------------
# decision.monthly_equivalent / pipeline_frame
# ---------------------------------------------------------------------------
def _queue() -> pd.DataFrame:
    return pd.DataFrame([
        {"ACTION_ID": "1", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 120.0, "PERIOD": "ANNUAL",
         "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "wh_a", "SEVERITY": "HIGH", "TITLE": "q1"},
        {"ACTION_ID": "2", "STATUS": "OPEN", "CONFIDENCE": 0.8, "ESTIMATED_USD": 50.0, "PERIOD": "monthly ",
         "SOURCE_ENTITY_TYPE": "QUERY_FINGERPRINT", "SOURCE_ENTITY_KEY": "abc", "SEVERITY": "LOW", "TITLE": "q2"},
        {"ACTION_ID": "3", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 500.0, "PERIOD": "ONE_TIME",
         "SOURCE_ENTITY_TYPE": "", "SOURCE_ENTITY_KEY": "", "SEVERITY": "LOW", "TITLE": "q3"},
        {"ACTION_ID": "4", "STATUS": "OPEN", "CONFIDENCE": None, "ESTIMATED_USD": None, "PERIOD": None,
         "SOURCE_ENTITY_TYPE": "", "SOURCE_ENTITY_KEY": "", "SEVERITY": "LOW", "TITLE": "q4"},
        {"ACTION_ID": "5", "STATUS": "OPEN", "CONFIDENCE": 0.7, "ESTIMATED_USD": 30.0, "PERIOD": "",
         "SOURCE_ENTITY_TYPE": "", "SOURCE_ENTITY_KEY": "", "SEVERITY": "LOW", "TITLE": "q5"},
        {"ACTION_ID": "6", "STATUS": "OPEN", "CONFIDENCE": 0.7, "ESTIMATED_USD": 0.0, "PERIOD": "MONTHLY",
         "SOURCE_ENTITY_TYPE": "", "SOURCE_ENTITY_KEY": "", "SEVERITY": "LOW", "TITLE": "q6"},
    ])


def test_monthly_equivalent_normalises_periods():
    frame, summary = monthly_equivalent(_queue())
    monthly = dict(zip(frame["ACTION_ID"], frame["MONTHLY_USD"], strict=True))
    assert monthly["1"] == 10.0 and monthly["2"] == 50.0                  # ANNUAL /12, MONTHLY x1
    for aid in ("3", "4", "5", "6"):                                     # one-time / unspecified / unpriced
        assert pd.isna(monthly[aid]), aid
    assert summary == {"items": 6, "monthly_usd": 60.0, "priced_count": 2, "annual_count": 1,
                       "unpriced_count": 2, "one_time_count": 1, "one_time_usd": 500.0,
                       "unspecified_count": 1, "unspecified_usd": 30.0}
    assert "MONTHLY_USD" not in _queue().columns                          # input never mutated
    for empty in (None, pd.DataFrame()):
        out, s = monthly_equivalent(empty)
        assert out.empty and "MONTHLY_USD" in out.columns and s["items"] == 0 and s["monthly_usd"] == 0.0


def test_pipeline_frame_unions_addressable_and_queued_without_double_counting():
    roll = rollup_savings([SavingsOpportunity("IDLE", "WH_A", 80.0, 0.6),
                           SavingsOpportunity("RESIZE", "WH_A", 50.0, 0.6),     # overlap: dropped by rollup
                           SavingsOpportunity("IDLE", "WH_B", 20.0, 0.3)])
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)                      # no all-NA concat deprecation
        pf = pipeline_frame(roll.items, _queue())
    addr = pf[pf["KIND"] == "Addressable"]
    assert list(addr["SOURCE_ENTITY_KEY"]) == ["WH_A", "WH_B"]
    assert set(addr["SOURCE_ENTITY_TYPE"]) == {"WAREHOUSE"} and set(addr["STATUS"]) == {"OPEN"}
    assert set(addr["PERIOD"]) == {"MONTHLY"} and list(addr["CONFIDENCE"]) == [0.6, 0.3]
    assert list(addr["ESTIMATED_USD"]) == [80.0, 20.0] == list(addr["MONTHLY_USD"])
    assert list(addr["TITLE"]) == ["Tighten auto-suspend on WH_A", "Tighten auto-suspend on WH_B"]
    q = pf[pf["KIND"] == "Queued"].set_index("ACTION_ID")
    assert q.loc["1", "ESTIMATED_USD"] == 10.0 and q.loc["1", "AUTHORED_USD"] == 120.0    # monthly basis
    assert pd.isna(q.loc["3", "ESTIMATED_USD"]) and q.loc["3", "AUTHORED_USD"] == 500.0   # one-time: out
    proj = scenario_projection(pf, adoption_pct=100, realization_pct=100, confidence_floor=0.6)
    # WH_A: addressable $80 and the queued wh_a $10/mo are ONE entity (largest wins); WH_B is below
    # the 0.6 floor; abc $50; the one-time and the unspecified rows project $0
    assert proj["gross_estimate"] == 80.0 + 50.0
    # candidates count status + confidence only: WH_A, abc, and the $0-projecting #3 / #5 / #6
    assert proj["candidates"] == 5.0
    # queued-only / addressable-only / neither
    assert set(pipeline_frame(None, _queue())["KIND"]) == {"Queued"}
    assert set(pipeline_frame(roll.items, None)["KIND"]) == {"Addressable"}
    assert pipeline_frame([], pd.DataFrame()).empty
    assert scenario_projection(pipeline_frame([], None), adoption_pct=60, realization_pct=70,
                               confidence_floor=0.6)["candidates"] == 0.0



def test_pipeline_frame_types_unread_rows_as_objects():
    """Next-Fifty #35: an UNREAD_MAINT opportunity targets an object FQN, so its synthetic row is an OBJECT (the
    Entity 360 type Storage & waste drills to): it de-duplicates against a queued OBJECT action on the same FQN and
    never against a warehouse that happens to share the key."""
    opps = [SavingsOpportunity("UNREAD_MAINT", "DB.S.T", 40.0, 0.6), SavingsOpportunity("IDLE", "WH_A", 10.0, 0.6)]
    pf = pipeline_frame(opps, None).set_index("SOURCE_ENTITY_KEY")
    assert pf.loc["DB.S.T", "SOURCE_ENTITY_TYPE"] == "OBJECT"
    assert pf.loc["DB.S.T", "TITLE"] == "Stop maintenance on unread DB.S.T"
    assert pf.loc["DB.S.T", "SOURCE"] == "Cost ▸ Optimization & Savings (UNREAD_MAINT)"
    assert pf.loc["WH_A", "SOURCE_ENTITY_TYPE"] == "WAREHOUSE"               # IDLE / RESIZE rows unchanged
    assert pf.loc["WH_A", "TITLE"] == "Tighten auto-suspend on WH_A"

    def queued(entity_type: str) -> pd.DataFrame:
        return pd.DataFrame([{"ACTION_ID": "9", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 25.0,
                              "PERIOD": "MONTHLY", "SOURCE_ENTITY_TYPE": entity_type,
                              "SOURCE_ENTITY_KEY": "db.s.t", "SEVERITY": "LOW", "TITLE": "q"}])

    same = scenario_projection(pipeline_frame(opps[:1], queued("OBJECT")), adoption_pct=100,
                               realization_pct=100, confidence_floor=0.6)
    assert same["candidates"] == 1.0 and same["gross_estimate"] == 40.0     # one object, the larger wins
    other = scenario_projection(pipeline_frame(opps[:1], queued("WAREHOUSE")), adoption_pct=100,
                                realization_pct=100, confidence_floor=0.6)
    assert other["candidates"] == 2.0 and other["gross_estimate"] == 65.0   # never merged with a warehouse

def test_pipeline_frame_accepts_an_already_normalised_queue():
    frame, _ = monthly_equivalent(_queue())
    pf = pipeline_frame([], frame)
    assert list(pf["ESTIMATED_USD"].head(2)) == [10.0, 50.0]


def test_no_verified_savings_enter_the_pipeline_frame():
    src = _src("app/logic/decision.py")
    body = src.split("def pipeline_frame(", 1)[1].split("\ndef ", 1)[0]
    assert "VERIFIED_USD" not in body.split('"""', 2)[2]
    assert re.search(r"Verified savings never\s+enter this frame", body)
