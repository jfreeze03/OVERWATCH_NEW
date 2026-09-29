"""Next-Fifty #31, the pandas side (pure): actions.split_reverted and the run-rate rollups that drop an undone
change, ledger_totals (the Proof fallback when the whole-ledger summary read fails -- the SAME rule as the SQL:
reverted rows leave the run-rate figures, never the estimate-accuracy ones), settle_schedule, and Proof's
per-item FLAGS / Reverted savings list / carried realization."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

import app.logic.actions as actions_mod
from app.logic import proof
from app.logic.actions import (
    ledger_totals,
    savings_by_lever,
    savings_by_month,
    savings_month_calendar,
    split_reverted,
)

_LTZ = "America/Chicago"


def test_split_reverted_tolerates_missing_column_nat_and_none():
    no_col = pd.DataFrame([{"STATE": "VERIFIED"}])
    kept, rev = split_reverted(no_col)
    assert len(kept) == 1 and rev.empty and list(rev.columns) == ["STATE"]
    frame = pd.DataFrame({"STATE": ["A", "B", "C", "D"],
                          "REVERTED_AT": pd.Series([None, pd.NaT, float("nan"),
                                                    pd.Timestamp("2026-08-01 06:40", tz=_LTZ)], dtype="object")})
    kept, rev = split_reverted(frame)
    assert list(kept["STATE"]) == ["A", "B", "C"] and list(rev["STATE"]) == ["D"]
    kept, rev = split_reverted(None)
    assert kept.empty and rev.empty
    empty = pd.DataFrame(columns=["STATE", "REVERTED_AT"])
    assert split_reverted(empty)[0] is empty


def _rows() -> pd.DataFrame:
    rv = pd.Timestamp("2026-08-01 06:40", tz=_LTZ)
    return pd.DataFrame([
        # kept: a hand-verified item with an estimate, this quarter
        {"ITEM_ID": "a", "STATE": "VERIFIED", "ESTIMATED_USD": 100.0, "VERIFIED_USD": 80.0,
         "CREATED_AT": "2026-07-01", "VERIFIED_AT": "2026-07-11", "SOURCE": "manual", "SOURCE_CHANGE_ID": None,
         "FINDING_TYPE": "SCHEDULE", "REVERTED_AT": None, "VOLUME_CONFOUNDED": None},
        # reverted: an adopted row with an estimate, this quarter, volume-confounded
        {"ITEM_ID": "b", "STATE": "VERIFIED", "ESTIMATED_USD": 50.0, "VERIFIED_USD": 60.0,
         "CREATED_AT": "2026-07-01", "VERIFIED_AT": "2026-07-16", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C2",
         "FINDING_TYPE": "RESIZE", "REVERTED_AT": rv, "REVERT_KIND": "partial", "VOLUME_CONFOUNDED": True},
        # reverted: an auto row without an estimate, June
        {"ITEM_ID": "c", "STATE": "VERIFIED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": 200.0,
         "CREATED_AT": "2026-06-01", "VERIFIED_AT": "2026-06-16", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C3",
         "FINDING_TYPE": "AUTO_SUSPEND", "REVERTED_AT": rv, "REVERT_KIND": "full", "VOLUME_CONFOUNDED": None},
        # kept: an auto row without an estimate, June
        {"ITEM_ID": "d", "STATE": "VERIFIED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": 30.0,
         "CREATED_AT": "2026-06-01", "VERIFIED_AT": "2026-06-16", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C4",
         "FINDING_TYPE": "AUTO_SUSPEND", "REVERTED_AT": None, "VOLUME_CONFOUNDED": None},
        # a still-measuring auto row the scan already saw undone: not pending, not in the pipeline
        {"ITEM_ID": "e", "STATE": "ESTIMATED", "ESTIMATED_USD": 25.0, "VERIFIED_USD": None,
         "CREATED_AT": "2026-09-10", "VERIFIED_AT": None, "SOURCE": "auto", "SOURCE_CHANGE_ID": "C5",
         "FINDING_TYPE": "RESIZE", "REVERTED_AT": rv, "REVERT_KIND": "full", "TRACKING_UNTIL": "2026-09-24"},
        # a still-measuring auto row: pending
        {"ITEM_ID": "f", "STATE": "ESTIMATED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": None,
         "CREATED_AT": "2026-09-20", "VERIFIED_AT": None, "SOURCE": "auto", "SOURCE_CHANGE_ID": "C6",
         "FINDING_TYPE": "RESIZE", "REVERTED_AT": None, "TRACKING_UNTIL": "2026-10-04"},
        # reverted but verified before the active window
        {"ITEM_ID": "g", "STATE": "VERIFIED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": 400.0,
         "CREATED_AT": "2025-05-01", "VERIFIED_AT": "2025-05-16", "SOURCE": "auto", "SOURCE_CHANGE_ID": "C7",
         "FINDING_TYPE": "RESIZE", "REVERTED_AT": rv, "REVERT_KIND": "full", "VOLUME_CONFOUNDED": None},
    ])


@pytest.fixture()
def _sep28(monkeypatch):
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 28, 9, 0))


def test_ledger_totals_drops_reverted_rows_from_the_run_rate_only(_sep28):
    t = ledger_totals(_rows())
    # run-rate figures: kept rows only (a, d)
    assert (t["verified_usd"], t["verified_count"]) == (110.0, 2)
    assert (t["verified_active_usd"], t["verified_active_count"]) == (110.0, 2)
    assert t["verified_qtd_usd"] == 80.0                           # a only (b is this quarter but undone)
    assert (t["volume_confounded_count"], t["volume_confounded_usd"]) == (0, 0.0)    # b is reverted
    assert (t["estimated_usd"], t["estimated_count"]) == (0.0, 1)   # f only; e is undone
    assert t["auto_settle_pending_count"] == 1
    # estimate-accuracy figures KEEP the reverted rows: a (80 of 100) + b (60 of 50)
    assert t["realized_verified_usd"] == 140.0 and t["realized_estimated_usd"] == 150.0
    assert t["realization_pct"] == round(140.0 / 150.0 * 100, 1)
    assert t["verified_estimated_usd"] == 150.0
    assert (t["verified_no_estimate_count"], t["verified_no_estimate_auto_count"]) == (3, 3)   # c, d, g
    assert t["avg_days_to_verify"] == round((10 + 15 + 15 + 15 + 15) / 5, 1)
    # disclosure
    assert (t["reverted_count"], t["reverted_usd"]) == (3, 660.0)                   # b, c, g
    assert (t["reverted_active_count"], t["reverted_active_usd"]) == (2, 260.0)     # b, c
    assert t["reverted_pending_count"] == 1                                         # e


def test_ledger_totals_new_keys_default_to_zero():
    for frame in (pd.DataFrame(), None):
        t = ledger_totals(frame)
        assert (t["reverted_count"], t["reverted_usd"], t["reverted_active_count"], t["reverted_active_usd"],
                t["reverted_pending_count"]) == (0, 0.0, 0, 0.0, 0)
    # an older read without REVERTED_AT: nothing is reverted, the figures are the pre-#31 ones
    legacy = _rows().drop(columns=["REVERTED_AT", "REVERT_KIND"])
    t = ledger_totals(legacy)
    assert t["verified_count"] == 5 and t["reverted_count"] == 0 and t["estimated_count"] == 2


def test_month_and_lever_rollups_drop_reverted_rows(_sep28):
    rows = _rows()
    cal = savings_month_calendar(rows, 12)
    assert float(cal["VERIFIED_USD"].sum()) == 110.0                  # a (Jul) + d (Jun); b / c undone
    assert float(cal.loc[cal["MONTH"] == "2026-07", "VERIFIED_USD"].iloc[0]) == 80.0
    assert float(savings_by_month(rows)["VERIFIED_USD"].sum()) == 110.0
    lever = savings_by_lever(rows).set_index("LEVER")
    assert set(lever.index) == {"SCHEDULE", "AUTO_SUSPEND"} and lever.loc["AUTO_SUSPEND", "VERIFIED_USD"] == 30.0
    # all reverted -> empty (never a $0 lever row)
    only_rev = rows[rows["ITEM_ID"].isin(["b", "c"])]
    assert savings_by_lever(only_rev).empty and savings_month_calendar(only_rev).empty


def test_settle_schedule_ignores_a_reverted_pending_row():
    out = proof.settle_schedule(_rows(), date(2026, 9, 28))
    assert set(out) == {"pending", "next", "overdue", "undated"}        # keys unchanged
    assert out == {"pending": 1, "next": date(2026, 10, 5), "overdue": 0, "undated": 0}
    # without the column the undone row would read as overdue
    legacy = proof.settle_schedule(_rows().drop(columns=["REVERTED_AT"]), date(2026, 9, 28))
    assert legacy["pending"] == 2 and legacy["overdue"] == 1


def _evidence_ledger() -> pd.DataFrame:
    return pd.DataFrame([
        {"ITEM_ID": "p", "STATE": "VERIFIED", "VERIFIED_USD": 300.0, "FINDING_TYPE": "RESIZE", "SOURCE": "auto",
         "SOURCE_CHANGE_ID": "C1", "CHANGE_WAREHOUSE": "WH_A", "CHANGE_SETTING": "SIZE",
         "CHANGE_OLD_VALUE": "Large", "CHANGE_NEW_VALUE": "Small", "CHANGE_VERDICT": "IMPROVED",
         "NOTES": "measured on the full window", "VERIFIED_AT": pd.Timestamp("2026-06-16 06:45"),
         "REVERTED_AT": pd.Timestamp("2026-08-01 06:40", tz=_LTZ), "REVERT_KIND": "full",
         "REVERT_OLD_VALUE": "Small", "REVERT_NEW_VALUE": "Medium", "REVERT_CHANGE_ID": "C1b",
         "VOLUME_CONFOUNDED": True},
        {"ITEM_ID": "q", "STATE": "VERIFIED", "VERIFIED_USD": 90.0, "FINDING_TYPE": "AUTO_SUSPEND",
         "SOURCE": "auto", "SOURCE_CHANGE_ID": "C2", "CHANGE_WAREHOUSE": "WH_B", "CHANGE_SETTING": "AUTO_SUSPEND",
         "CHANGE_OLD_VALUE": "600", "CHANGE_NEW_VALUE": "60", "CHANGE_VERDICT": "IMPROVED",
         "NOTES": "measured on the full window", "VERIFIED_AT": pd.Timestamp("2026-06-16 06:45"),
         # an LTZ read back in UTC: 03:00 UTC on Sep 2 is Sep 1 22:00 account time
         "REVERTED_AT": pd.Timestamp("2026-09-02 03:00", tz="UTC"), "REVERT_KIND": "full",
         "REVERT_OLD_VALUE": "60", "REVERT_NEW_VALUE": None, "REVERT_CHANGE_ID": "C2b"},
        {"ITEM_ID": "r", "STATE": "VERIFIED", "VERIFIED_USD": 500.0, "FINDING_TYPE": "RESIZE", "SOURCE": "auto",
         "SOURCE_CHANGE_ID": "C3", "CHANGE_WAREHOUSE": "WH_X", "CHANGE_SETTING": "SIZE",
         "CHANGE_OLD_VALUE": "6X-Large", "CHANGE_NEW_VALUE": "4X-Large", "CHANGE_VERDICT": "IMPROVED",
         "NOTES": "measured on the full window", "VERIFIED_AT": pd.Timestamp("2026-06-16 06:45"),
         "REVERTED_AT": pd.Timestamp("2026-07-01 06:40", tz=_LTZ), "REVERT_KIND": "partial",
         "REVERT_OLD_VALUE": "4X-Large", "REVERT_NEW_VALUE": "5X-Large", "REVERT_CHANGE_ID": "C3b"},
        {"ITEM_ID": "s", "STATE": "VERIFIED", "VERIFIED_USD": 40.0, "FINDING_TYPE": "SCHEDULE", "SOURCE": "manual",
         "SOURCE_CHANGE_ID": None, "TARGET_OBJECT": "WH_S", "NOTES": "booked from Optimize",
         "VERIFIED_AT": pd.Timestamp("2026-07-01 10:00"), "REVERTED_AT": None},
        # a superseded manual twin never shows (its auto row is the booking of record)
        {"ITEM_ID": "t", "STATE": "VERIFIED", "VERIFIED_USD": 150.0, "FINDING_TYPE": "RESIZE", "SOURCE": "manual",
         "TARGET_OBJECT": "WH_A", "SUPERSEDED_BY_CHANGE_ID": "C1", "NOTES": "manual",
         "VERIFIED_AT": pd.Timestamp("2026-06-20 10:00"), "REVERTED_AT": pd.Timestamp("2026-08-01 06:40", tz=_LTZ)},
    ])


def test_evidence_flags_lead_with_the_revert():
    ev = proof.evidence_rows(_evidence_ledger(), None, date(2026, 9, 28)).set_index("TARGET")
    assert len(ev) == 4                                             # rows are KEPT (flagged), twin excluded
    assert ev.loc["WH_A", "FLAGS"] == "reverted Aug 1 → Medium · volume-confounded"
    assert ev.loc["WH_B", "FLAGS"] == "reverted Sep 1 → never"      # NULL auto-suspend = never suspends
    assert ev.loc["WH_X", "FLAGS"] == "partly reverted Jul 1 → 5X-Large"
    assert ev.loc["WH_S", "FLAGS"] is None
    # a frame without the revert columns reads exactly as before
    legacy = _evidence_ledger().drop(columns=["REVERTED_AT", "REVERT_KIND", "REVERT_NEW_VALUE"])
    ev2 = proof.evidence_rows(legacy, None, date(2026, 9, 28)).set_index("TARGET")
    assert ev2.loc["WH_A", "FLAGS"] == "volume-confounded" and ev2.loc["WH_B", "FLAGS"] is None
    assert list(proof.evidence_rows(legacy, None, date(2026, 9, 28)).columns) == list(proof.EVIDENCE_COLUMNS)


def test_reverted_rows_list():
    rv = proof.reverted_rows(_evidence_ledger())
    assert list(rv.columns) == list(proof.REVERTED_COLUMNS)
    assert list(rv["TARGET"]) == ["WH_B", "WH_A", "WH_X"]            # newest revert first; twin excluded
    b = rv.iloc[0]
    assert (b["CHANGE"], b["REVERTED_TO"], b["REVERT"]) == ("600 → 60", "60 → never", "Full")
    assert b["REVERTED_AT"] == pd.Timestamp("2026-09-01 22:00")     # tz-naive account time
    assert rv["REVERTED_AT"].dt.tz is None
    x = rv.iloc[2]
    assert (x["LEVER"], x["CHANGE"], x["REVERTED_TO"], x["REVERT"], x["STATE"], x["VERIFIED_USD"]) == (
        "RESIZE", "6X-Large → 4X-Large", "4X-Large → 5X-Large", "Partial", "VERIFIED", 500.0)
    for empty in (None, pd.DataFrame(), _evidence_ledger().drop(columns=["REVERTED_AT"]),
                  _evidence_ledger().iloc[[3]]):
        out = proof.reverted_rows(empty)
        assert out.empty and list(out.columns) == list(proof.REVERTED_COLUMNS)
    # a shaped-harness style frame (floats where strings are expected) never raises
    shaped = pd.DataFrame({"STATE": [1.0, 2.0], "VERIFIED_USD": [1.0, 2.0], "REVERT_KIND": [1.0, 2.0],
                           "CHANGE_SETTING": [1.0, 2.0], "REVERT_NEW_VALUE": [1.0, 2.0],
                           "REVERTED_AT": [pd.Timestamp("2026-08-15"), pd.Timestamp("2026-08-16")]})
    out = proof.reverted_rows(shaped)
    assert len(out) == 2 and out["REVERT"].isna().all()


def test_carried_realization_still_counts_a_reverted_row():
    rows = _rows()
    carried = proof.carried_realization(rows)
    assert carried is not None
    # a (100 -> 80) and b (50 -> 60, reverted) both count: accuracy, not persistence
    assert (carried["estimated_usd"], carried["realized_usd"], carried["items"]) == (150.0, 140.0, 2)


# --------------------------------------------------------------------------- review r1 fixes (pure)
def test_lever_realization_keeps_reverted_rows_like_the_headline(_sep28):
    # review r1 F10: one lever, one kept item (est 100 -> 80) and one the scan saw undone (est 100 -> 60).
    # The $ / item columns are the run-rate view (the kept item only); the realization is estimate
    # accuracy and keeps the undone item, exactly as the headline Realization rate does.
    rv = pd.Timestamp("2026-08-01 06:40", tz=_LTZ)
    rows = pd.DataFrame([
        {"ITEM_ID": "k", "STATE": "VERIFIED", "ESTIMATED_USD": 100.0, "VERIFIED_USD": 80.0,
         "VERIFIED_AT": "2026-07-11", "FINDING_TYPE": "RESIZE", "REVERTED_AT": None},
        {"ITEM_ID": "r", "STATE": "VERIFIED", "ESTIMATED_USD": 100.0, "VERIFIED_USD": 60.0,
         "VERIFIED_AT": "2026-07-16", "FINDING_TYPE": "RESIZE", "REVERTED_AT": rv},
    ])
    lever = savings_by_lever(rows)
    assert list(lever["LEVER"]) == ["RESIZE"]
    row = lever.iloc[0]
    assert (row["VERIFIED_USD"], row["ITEMS"]) == (80.0, 1)
    headline = ledger_totals(rows)["realization_pct"]
    assert headline == 70.0 and row["REALIZATION_PCT"] == headline          # (80 + 60) / (100 + 100)
    # without the revert column nothing is undone: the pre-#31 figures
    legacy = savings_by_lever(rows.drop(columns=["REVERTED_AT"])).iloc[0]
    assert (legacy["VERIFIED_USD"], legacy["ITEMS"], legacy["REALIZATION_PCT"]) == (140.0, 2, 70.0)


@pytest.mark.parametrize(("value", "setting", "shown"), [
    (None, "AUTO_SUSPEND", "never"), ("", "AUTO_SUSPEND", "never"), ("0", "AUTO_SUSPEND", "never"),
    ("0.0", "AUTO_SUSPEND", "never"), ("-1", "AUTO_SUSPEND", "never"), (0.0, "auto_suspend", "never"),
    ("60", "AUTO_SUSPEND", "60"), ("abc", "AUTO_SUSPEND", "abc"),
    ("0", "MAX_CLUSTERS", "0"), (None, "SIZE", "?"), ("Large", "SIZE", "Large"),
])
def test_undone_value_matches_the_sql_rank_rule(value, setting, shown):
    # review r1 F9: the SQL cost rank reads AUTO_SUSPEND NULL OR <= 0 as "never suspends"; so does the label
    assert proof._undone_value(value, setting) == shown


def _zero_suspend_ledger() -> pd.DataFrame:
    return pd.DataFrame([
        # booked 0 (never suspends) -> 60, later undone back to 0
        {"ITEM_ID": "z", "STATE": "VERIFIED", "VERIFIED_USD": 75.0, "FINDING_TYPE": "AUTO_SUSPEND",
         "SOURCE": "auto", "SOURCE_CHANGE_ID": "Z1", "CHANGE_WAREHOUSE": "WH_Z", "CHANGE_SETTING": "AUTO_SUSPEND",
         "CHANGE_OLD_VALUE": "0", "CHANGE_NEW_VALUE": "60", "CHANGE_VERDICT": "IMPROVED",
         "NOTES": "measured on the full window", "VERIFIED_AT": pd.Timestamp("2026-06-16 06:45"),
         "REVERTED_AT": pd.Timestamp("2026-08-20 06:40", tz=_LTZ), "REVERT_KIND": "full",
         "REVERT_OLD_VALUE": "60", "REVERT_NEW_VALUE": "0", "REVERT_CHANGE_ID": "Z2",
         "REVERT_SETTING": "AUTO_SUSPEND"},
    ])


def test_a_zero_second_auto_suspend_reads_never_everywhere():
    ev = proof.evidence_rows(_zero_suspend_ledger(), None, date(2026, 9, 28)).iloc[0]
    assert ev["FLAGS"] == "reverted Aug 20 → never"                  # never "→ 0"
    assert ev["CHANGE"] == "never → 60"
    rv = proof.reverted_rows(_zero_suspend_ledger()).iloc[0]
    assert (rv["CHANGE"], rv["REVERTED_TO"]) == ("never → 60", "60 → never")


def _inherited_ledger() -> pd.DataFrame:
    base = {"STATE": "VERIFIED", "SOURCE": "auto", "CHANGE_VERDICT": "IMPROVED",
            "NOTES": "measured on the full window", "VERIFIED_AT": pd.Timestamp("2026-06-16 06:45"),
            "REVERTED_AT": pd.Timestamp("2026-07-10 06:40", tz=_LTZ)}
    return pd.DataFrame([
        # the RN=1 AUTO_SUSPEND row inherits its $0 SIZE partner's revert (one measured window)
        {**base, "ITEM_ID": "p1", "VERIFIED_USD": 200.0, "FINDING_TYPE": "AUTO_SUSPEND", "SOURCE_CHANGE_ID": "P1",
         "CHANGE_WAREHOUSE": "WH_P", "CHANGE_SETTING": "AUTO_SUSPEND", "CHANGE_OLD_VALUE": "600",
         "CHANGE_NEW_VALUE": "60", "REVERT_KIND": "partial", "REVERT_OLD_VALUE": "Medium",
         "REVERT_NEW_VALUE": "Large", "REVERT_CHANGE_ID": "P3", "REVERT_SETTING": "SIZE"},
        # the partner's own revert reads as before
        {**base, "ITEM_ID": "p2", "VERIFIED_USD": 0.0, "FINDING_TYPE": "RESIZE", "SOURCE_CHANGE_ID": "P2",
         "CHANGE_WAREHOUSE": "WH_P2", "CHANGE_SETTING": "SIZE", "CHANGE_OLD_VALUE": "Large",
         "CHANGE_NEW_VALUE": "Medium", "REVERT_KIND": "full", "REVERT_OLD_VALUE": "Medium",
         "REVERT_NEW_VALUE": "Large", "REVERT_CHANGE_ID": "P3", "REVERT_SETTING": "SIZE"},
        # a SIZE row whose AUTO_SUSPEND partner went to NULL (never): the partner's setting decides 'never'
        {**base, "ITEM_ID": "q1", "VERIFIED_USD": 90.0, "FINDING_TYPE": "RESIZE", "SOURCE_CHANGE_ID": "Q1",
         "CHANGE_WAREHOUSE": "WH_Q", "CHANGE_SETTING": "SIZE", "CHANGE_OLD_VALUE": "Large",
         "CHANGE_NEW_VALUE": "Medium", "REVERT_KIND": "partial", "REVERT_OLD_VALUE": "60",
         "REVERT_NEW_VALUE": None, "REVERT_CHANGE_ID": "Q3", "REVERT_SETTING": "AUTO_SUSPEND"},
    ])


def test_an_inherited_revert_names_the_partner_setting():
    ev = proof.evidence_rows(_inherited_ledger(), None, date(2026, 9, 28)).set_index("TARGET")
    assert ev.loc["WH_P", "FLAGS"] == "partly reverted Jul 10 (co-attributed SIZE → Large)"
    assert ev.loc["WH_P2", "FLAGS"] == "reverted Jul 10 → Large"
    assert ev.loc["WH_Q", "FLAGS"] == "partly reverted Jul 10 (co-attributed AUTO_SUSPEND → never)"
    rv = proof.reverted_rows(_inherited_ledger()).set_index("TARGET")
    assert (rv.loc["WH_P", "CHANGE"], rv.loc["WH_P", "REVERTED_TO"], rv.loc["WH_P", "REVERT"]) == (
        "600 → 60", "SIZE: Medium → Large", "Partial")
    assert rv.loc["WH_P2", "REVERTED_TO"] == "Medium → Large"
    assert rv.loc["WH_Q", "REVERTED_TO"] == "AUTO_SUSPEND: 60 → never"
    # review r2: 'Full' through a partner's earlier revert names both facts -- never 'reverted … (co-attributed
    # MAX_CLUSTERS → 3)' as if that change fully undid a SIZE downsize
    full = _inherited_ledger()
    full.loc[full["CHANGE_WAREHOUSE"] == "WH_P", "REVERT_KIND"] = "full"
    ev_full = proof.evidence_rows(full, None, date(2026, 9, 28)).set_index("TARGET")
    assert ev_full.loc["WH_P", "FLAGS"] == ("reverted Jul 10 via co-attributed SIZE → Large; "
                                            "AUTO_SUSPEND since fully undone")
    # review r3: the flag makes no run-rate claim, so a REJECTED (never counted) row reads the same way
    rejected = full.copy()
    rejected.loc[rejected["CHANGE_WAREHOUSE"] == "WH_P", "STATE"] = "REJECTED"
    flags = proof.evidence_rows(rejected, None, date(2026, 9, 28)).set_index("TARGET").loc["WH_P", "FLAGS"]
    assert flags == "reverted Jul 10 via co-attributed SIZE → Large; AUTO_SUSPEND since fully undone"
    assert "run-rate" not in flags
    rv_full = proof.reverted_rows(full).set_index("TARGET")
    assert (rv_full.loc["WH_P", "REVERTED_TO"], rv_full.loc["WH_P", "REVERT"]) == (
        "SIZE: Medium → Large (AUTO_SUSPEND since fully undone)", "Full")
    # an older read without REVERT_SETTING reads the row's own setting (the pre-fix text)
    legacy = _inherited_ledger().drop(columns=["REVERT_SETTING"])
    assert proof.reverted_rows(legacy).set_index("TARGET").loc["WH_P", "REVERTED_TO"] == "Medium → Large"
