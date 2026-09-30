"""#42 Part 1 — app.logic.showback.company_showback (pure).

The one law: company rows + the cloud-services adjustment row + the unattributed row add up to the
all-in total (billed metering + estimated storage) exactly, and each column adds up to its billed
family. The adjustment is its own account row (never taken out of a company); UNKNOWN is a company
row; storage is tier-priced; a negative family residual is kept and named; coverage gaps are named
from the real first/last days; the non-ok states never raise and never read as a clean $0.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd
import pytest

from app.config import DEFAULT_SETTINGS
from app.logic import cost_coverage, showback
from app.logic.formulas import DEFAULT_STORAGE_USD_PER_TB_MONTH, format_usd

RATE = 3.68
AI_RATE = 2.20
RATES = showback.storage_tier_rates(DEFAULT_SETTINGS)

_METERING = [  # SERVICE_TYPE, billed credits, CS adjustment credits
    ("WAREHOUSE_METERING", 1000.0, -50.0),
    ("WAREHOUSE_METERING_READER", 20.0, 0.0),
    ("SERVERLESS_TASK", 100.0, 0.0),
    ("AUTO_CLUSTERING", 40.0, 0.0),
    ("AI_SERVICES", 200.0, 0.0),
    ("REPLICATION", 10.0, 0.0),
]
_WAREHOUSE = {"ALFA": 700.0, "Trexis": 250.0, "UNKNOWN": 60.0}
_SERVERLESS = [("ALFA", "CLUSTERING", 30.0), ("ALFA", "SERVERLESS_TASK", 50.0), ("Trexis", "SERVERLESS_TASK", 40.0)]
_COCO = [("ALFA", 60.0), ("ALFA", 20.0), ("Trexis", 50.0)]          # one row per user
_STORAGE_DB = {"ALFA": 2.0, "Trexis": 1.0, "UNKNOWN": 0.5}          # TiB-months
_STORAGE_ACCT = {"TABLE": 3.0, "STAGE": 0.2, "FAILSAFE": 0.6, "HYBRID": 0.01, "ARCHIVE_COOL": 0.0,
                 "ARCHIVE_COLD": 0.0}
_COVERAGE = {
    "FACT_METERING_DAILY": ("2025-01-01", "2026-09-30"),
    "FACT_WAREHOUSE_DAILY": ("2025-01-01", "2026-09-30"),
    "FACT_OBJECT_COST_DAILY": ("2026-06-30", "2026-09-30"),
    "FACT_AI_USAGE_DAILY": ("2026-04-06", "2026-09-29"),
    "FACT_STORAGE_DAILY": ("2025-01-01", "2026-09-29"),
    "FACT_STORAGE_ACCOUNT_DAILY": ("2025-01-01", "2026-09-29"),
}


def _row(kind, company=None, service=None, credits=None, adj=None, tib=None, first=None, last=None, n=None,
         loaded=None):
    return {"LINE_KIND": kind, "COMPANY": company, "SERVICE_TYPE": service, "CREDITS": credits,
            "CREDITS_ADJUSTMENT": adj, "TIB_MO": tib,
            "FIRST_DAY": pd.Timestamp(first) if first else pd.NaT,
            "LAST_DAY": pd.Timestamp(last) if last else pd.NaT, "DAYS_IN_SPAN": n,
            "LOADED_AT": pd.Timestamp(loaded) if loaded else pd.NaT}


def _synthetic_frame(*, window=("2026-08-31", "2026-09-29"), span=("2026-08-31", "2026-09-29", 30),
                     storage_span=("2026-08-31", "2026-09-29", 30), metering=None, warehouse=None,
                     serverless=None, coco=None, storage_db=None, storage_acct=None, coverage=None,
                     loaded=None, only: str | None = None) -> pd.DataFrame:
    """The builder's long frame. ``only`` keeps the keyed legs of one company (a scoped read);
    ``loaded`` maps a COVERAGE table to its LOADED_AT (the builder's MAX(LOAD_TS))."""
    rows = [_row("WINDOW", first=window[0], last=window[1]),
            _row("SPAN", first=span[0], last=span[1], n=span[2]),
            _row("STORAGE_SPAN", first=storage_span[0], last=storage_span[1], n=storage_span[2])]
    rows += [_row("METERING", service=s, credits=c, adj=a) for s, c, a in (metering or _METERING)]
    keep = (lambda co: co == only) if only else (lambda co: True)
    rows += [_row("WAREHOUSE", company=co, credits=c)
             for co, c in (warehouse if warehouse is not None else _WAREHOUSE).items() if keep(co)]
    rows += [_row("SERVERLESS", company=co, service=arm, credits=c)
             for co, arm, c in (serverless if serverless is not None else _SERVERLESS) if keep(co)]
    rows += [_row("COCO", company=co, credits=c) for co, c in (coco if coco is not None else _COCO) if keep(co)]
    rows += [_row("STORAGE_DB", company=co, tib=t)
             for co, t in (storage_db if storage_db is not None else _STORAGE_DB).items() if keep(co)]
    if storage_span[2]:
        rows += [_row("STORAGE_ACCT", service=tier, tib=t)
                 for tier, t in (storage_acct if storage_acct is not None else _STORAGE_ACCT).items()]
    for table, (first, last) in (coverage if coverage is not None else _COVERAGE).items():
        rows.append(_row("COVERAGE", service=table, first=first, last=last, loaded=(loaded or {}).get(table)))
    return pd.DataFrame(rows, columns=list(showback.FRAME_COLUMNS))


def _run(frame, company="ALL"):
    return showback.company_showback(frame, rate=RATE, ai_rate=AI_RATE, storage_rates=RATES, company=company)


def _by_company(out) -> pd.DataFrame:
    return out["table"].set_index("COMPANY")


def _storage_est() -> float:
    return sum(t * RATES[tier] for tier, t in _STORAGE_ACCT.items())


def _billed() -> float:
    return sum(c * (AI_RATE if s == "AI_SERVICES" else RATE) for s, c, _ in _METERING)


def test_rows_tie_out_to_the_all_in_total_exactly():
    out = _run(_synthetic_frame())
    s = out["summary"]
    assert out["state"] == "ok" and s["state"] == "ok"
    assert s["allin_total_usd"] == pytest.approx(_billed() + _storage_est())
    assert out["table"]["TOTAL_USD"].sum() == pytest.approx(s["allin_total_usd"])
    assert (s["company_usd"] + s["adjustment_usd"] + s["unattributed_usd"]
            == pytest.approx(s["allin_total_usd"]))
    assert list(out["table"].columns) == list(showback.TABLE_COLUMNS)
    # R1-12: shares are of the spend before the cloud-services adjustment (company rows + unattributed),
    # so those rows sum to 100% and the adjustment row carries no share
    t = _by_company(out)
    assert s["share_basis_usd"] == pytest.approx(s["allin_total_usd"] - s["adjustment_usd"])
    assert s["share_basis_usd"] == pytest.approx(s["company_usd"] + s["unattributed_usd"])
    assert math.isnan(t.loc[showback.ADJUSTMENT_ROW, "SHARE_OF_TOTAL_PCT"])
    assert t["SHARE_OF_TOTAL_PCT"].sum(skipna=True) == pytest.approx(100.0)


def test_each_column_sums_to_its_billed_family():
    t = _run(_synthetic_frame())["table"]
    assert t["WAREHOUSE_USD"].sum() == pytest.approx(1000.0 * RATE)          # billed = used + adjustment
    assert t["SERVERLESS_USD"].sum() == pytest.approx((100.0 + 40.0) * RATE)
    assert t["AI_USD"].sum() == pytest.approx(200.0 * AI_RATE)
    assert t["OTHER_METERED_USD"].sum(skipna=True) == pytest.approx((20.0 + 10.0) * RATE)
    assert t["STORAGE_EST_USD"].sum(skipna=True) == pytest.approx(_storage_est())
    # company rows carry no other-metered dollars (no company key there): blank, not $0
    companies = t[~t["COMPANY"].isin((showback.ADJUSTMENT_ROW, showback.UNATTRIBUTED_ROW))]
    assert companies["OTHER_METERED_USD"].isna().all()


def test_ai_family_at_ai_rate_rest_at_compute_rate():
    out = _run(_synthetic_frame())
    assert out["summary"]["billed_credit_usd"] == pytest.approx((1000 + 20 + 100 + 40 + 10) * RATE + 200 * AI_RATE)
    alfa = _by_company(out).loc["ALFA"]
    assert alfa["AI_USD"] == pytest.approx(80.0 * AI_RATE)                   # per-user rows summed
    ai_row = out["breakdown"].set_index("SERVICE_FAMILY").loc["AI / Cortex"]
    assert ai_row["ACCOUNT_USD"] == pytest.approx(200.0 * AI_RATE)


def test_adjustment_is_an_account_row_never_taken_from_a_company():
    out = _run(_synthetic_frame())
    t = _by_company(out)
    assert t.loc["ALFA", "WAREHOUSE_USD"] == pytest.approx(700.0 * RATE)     # exact metering, pre-adjustment
    adj = t.loc[showback.ADJUSTMENT_ROW]
    assert adj["TOTAL_USD"] == pytest.approx(-50.0 * RATE)
    assert adj["WAREHOUSE_USD"] == pytest.approx(-50.0 * RATE)
    assert adj["SERVERLESS_USD"] == 0 and adj["AI_USD"] == 0 and adj["OTHER_METERED_USD"] == 0
    assert math.isnan(adj["STORAGE_EST_USD"])
    assert out["summary"]["adjustment_usd"] == pytest.approx(-50.0 * RATE)
    # the unattributed warehouse cell is billed - company rows - adjustment
    assert t.loc[showback.UNATTRIBUTED_ROW, "WAREHOUSE_USD"] == pytest.approx((1000 - 1010 + 50) * RATE)


def test_unknown_is_a_company_row_and_counts_in_the_share():
    out = _run(_synthetic_frame())
    order = out["table"]["COMPANY"].tolist()
    assert order == ["ALFA", "Trexis", showback.UNKNOWN_ROW, showback.ADJUSTMENT_ROW, showback.UNATTRIBUTED_ROW]
    unknown = _by_company(out).loc[showback.UNKNOWN_ROW]
    assert unknown["TOTAL_USD"] == pytest.approx(60.0 * RATE + 0.5 * RATES["TABLE"])
    s = out["summary"]
    company_total = out["table"]["TOTAL_USD"].iloc[:3].sum()
    assert s["company_usd"] == pytest.approx(company_total)
    assert s["company_share_pct"] == pytest.approx(company_total / s["share_basis_usd"] * 100.0)
    # the KPI is the company rows' own table shares added up, and the unattributed row is its complement
    assert s["company_share_pct"] == pytest.approx(out["table"]["SHARE_OF_TOTAL_PCT"].iloc[:3].sum())
    assert (s["company_share_pct"] + _by_company(out).loc[showback.UNATTRIBUTED_ROW, "SHARE_OF_TOTAL_PCT"]
            == pytest.approx(100.0))


def test_unknown_stays_last_among_companies_even_when_largest():
    out = _run(_synthetic_frame(warehouse={"ALFA": 10.0, "UNKNOWN": 900.0}))
    assert out["table"]["COMPANY"].tolist()[:3] == ["ALFA", "Trexis", showback.UNKNOWN_ROW]


def test_breakdown_unattributed_sums_to_the_unattributed_row():
    for frame in (_synthetic_frame(), _synthetic_frame(coco=[("ALFA", 500.0)]),
                  _synthetic_frame(metering=[("WAREHOUSE_METERING", 1000.0, -50.0)])):
        out = _run(frame)
        b = out["breakdown"]
        assert list(b.columns) == list(showback.BREAKDOWN_COLUMNS)
        un = _by_company(out).loc[showback.UNATTRIBUTED_ROW, "TOTAL_USD"]
        assert b["UNATTRIBUTED_USD"].sum() == pytest.approx(un)
        assert ((b["ACCOUNT_USD"] - b["COMPANY_ROWS_USD"] - b["ADJUSTMENT_USD"]).tolist()
                == pytest.approx(b["UNATTRIBUTED_USD"].tolist()))
        assert b["UNATTRIBUTED_USD"].is_monotonic_decreasing
        assert b["CONTENTS"].astype(str).str.len().gt(0).all()


def test_storage_is_tier_priced_and_residual_is_account_minus_databases():
    out = _run(_synthetic_frame())
    expected = (3.0 * RATES["TABLE"] + 0.2 * RATES["STAGE"] + 0.6 * RATES["FAILSAFE"]
                + 0.01 * RATES["HYBRID"])
    assert out["summary"]["storage_est_usd"] == pytest.approx(expected)
    assert RATES["HYBRID"] == DEFAULT_SETTINGS["STORAGE_HYBRID_USD_PER_TB_MONTH"] != RATES["TABLE"]
    t = _by_company(out)
    assert t.loc["ALFA", "STORAGE_EST_USD"] == pytest.approx(2.0 * RATES["TABLE"])
    assert t.loc[showback.UNATTRIBUTED_ROW, "STORAGE_EST_USD"] == pytest.approx(expected - 3.5 * RATES["TABLE"])
    st = out["breakdown"].set_index("SERVICE_FAMILY").loc[showback.STORAGE_FAMILY]
    assert st["ACCOUNT_USD"] == pytest.approx(expected) and st["ADJUSTMENT_USD"] == 0


def test_company_scope_returns_only_that_company_and_share_of_account_total():
    full = _run(_synthetic_frame())
    alfa_total = _by_company(full).loc["ALFA", "TOTAL_USD"]
    out = _run(_synthetic_frame(only="ALFA"), company="ALFA")
    assert out["state"] == "ok"
    assert out["table"]["COMPANY"].tolist() == ["ALFA"]
    assert out["breakdown"].empty
    s = out["summary"]
    assert s["scoped"] is True and s["unattributed_usd"] is None
    assert s["company_usd"] == pytest.approx(alfa_total)
    assert s["allin_total_usd"] == pytest.approx(full["summary"]["allin_total_usd"])
    # the scoped basis is the whole account's (metering is never scoped), the same as the ALL view's
    assert s["share_basis_usd"] == pytest.approx(full["summary"]["share_basis_usd"])
    assert s["company_share_pct"] == pytest.approx(alfa_total / full["summary"]["share_basis_usd"] * 100.0)
    # R1-23: the displayed (and exported) table column is the account share too, never the company's own 100%
    assert out["table"].loc[0, "SHARE_OF_TOTAL_PCT"] == pytest.approx(s["company_share_pct"])
    assert out["table"].loc[0, "SHARE_OF_TOTAL_PCT"] == pytest.approx(
        _by_company(full).loc["ALFA", "SHARE_OF_TOTAL_PCT"])
    assert out["table"].loc[0, "SHARE_OF_TOTAL_PCT"] < 100.0
    # a stray other-company row (never expected from a scoped read) is still dropped
    stray = _run(_synthetic_frame(), company="ALFA")
    assert stray["table"]["COMPANY"].tolist() == ["ALFA"]
    unknown = _run(_synthetic_frame(only="UNKNOWN"), company="UNKNOWN")
    assert unknown["table"]["COMPANY"].tolist() == [showback.UNKNOWN_ROW]
    empty = _run(_synthetic_frame(only="Trexis", warehouse={}, serverless=[], coco=[], storage_db={}),
                 company="Trexis")
    assert empty["state"] == "ok" and empty["table"].empty


def test_negative_family_residual_is_kept_and_named():
    out = _run(_synthetic_frame(coco=[("ALFA", 300.0)]))                 # 300 AI credits vs 200 billed
    b = out["breakdown"].set_index("SERVICE_FAMILY")
    assert b.loc["AI / Cortex", "UNATTRIBUTED_USD"] == pytest.approx(-100.0 * AI_RATE)
    assert _by_company(out).loc[showback.UNATTRIBUTED_ROW, "AI_USD"] == pytest.approx(-100.0 * AI_RATE)
    note = next(n for n in out["notes"] if n.startswith("AI / Cortex:"))
    assert f"by {format_usd(100.0 * AI_RATE)} in this span" in note
    assert "different Snowflake views with different day boundaries" in note
    # the tie-out still holds with a negative remainder
    assert out["table"]["TOTAL_USD"].sum() == pytest.approx(out["summary"]["allin_total_usd"])
    # a sub-dollar negative is rounding noise: no note
    tiny = _run(_synthetic_frame(coco=[("ALFA", 200.2)]))
    assert not any(n.startswith("AI / Cortex:") for n in tiny["notes"])


def test_keyed_dollars_with_no_metering_family_still_reconcile():
    out = _run(_synthetic_frame(metering=[("WAREHOUSE_METERING", 1000.0, -50.0)]))
    b = out["breakdown"].set_index("SERVICE_FAMILY")
    assert {"Serverless", "AI / Cortex"} <= set(b.index)
    assert b.loc["Serverless", "ACCOUNT_USD"] == 0


def test_no_metering_ever_is_no_ledger_state():
    cov = dict(_COVERAGE)
    cov["FACT_METERING_DAILY"] = (None, None)
    out = _run(_synthetic_frame(span=(None, None, 0), storage_span=(None, None, 0), metering=[], coverage=cov))
    assert out["state"] == "no_ledger" and out["table"].empty
    assert out["summary"]["span_label"] is None and isinstance(out["notes"], list)


def test_empty_span_is_no_basis_state():
    cov = dict(_COVERAGE)
    cov["FACT_METERING_DAILY"] = ("2025-01-01", "2026-09-30")
    out = _run(_synthetic_frame(window=("2026-09-30", "2026-09-29"), span=(None, None, 0),
                                storage_span=(None, None, 0), metering=[], coverage=cov))
    assert out["state"] == "no_basis" and out["table"].empty
    assert out["summary"]["stall_day"] is None                            # metering is current
    cov["FACT_METERING_DAILY"] = ("2025-01-01", "2026-09-20")
    stalled = _run(_synthetic_frame(window=("2026-09-23", "2026-09-29"), span=(None, None, 0),
                                    storage_span=(None, None, 0), metering=[], coverage=cov))
    assert stalled["state"] == "no_basis"
    assert stalled["summary"]["stall_day"] == "Sep 20, 2026"
    # one day behind the Window's end is normal lag, not a stall
    cov["FACT_METERING_DAILY"] = ("2025-01-01", "2026-09-28")
    lag = _run(_synthetic_frame(window=("2026-09-29", "2026-09-29"), span=(None, None, 0),
                                storage_span=(None, None, 0), metering=[], coverage=cov))
    assert lag["state"] == "no_basis" and lag["summary"]["stall_day"] is None


def test_missing_columns_is_shape_state_never_raises():
    frame = _synthetic_frame().drop(columns=["TIB_MO", "DAYS_IN_SPAN"])
    out = _run(frame)
    assert out["state"] == "shape" and out["summary"]["missing"] == ["DAYS_IN_SPAN", "TIB_MO"]
    assert out["table"].empty and list(out["table"].columns) == list(showback.TABLE_COLUMNS)
    assert _run(None)["state"] == "shape"
    assert _run(pd.DataFrame())["state"] == "shape"
    no_window = _synthetic_frame()
    no_window = no_window[no_window["LINE_KIND"] != "WINDOW"]
    assert _run(no_window)["state"] == "shape"


def test_shaped_junk_frame_never_raises():
    # the page-shaped harness hands every column a synthetic value (no WINDOW row)
    shaped = pd.DataFrame({c: [f"{c}_{i}" if c in ("LINE_KIND", "COMPANY", "SERVICE_TYPE") else float(i + 1)
                               for i in range(2)] for c in showback.FRAME_COLUMNS})
    assert _run(shaped)["state"] == "shape"
    # right columns, garbage cells: a broken read is 'shape', never "no metering yet" or a $0 row
    bad_num = _synthetic_frame()
    bad_num["CREDITS"] = bad_num["CREDITS"].astype(object)
    bad_num.loc[bad_num["LINE_KIND"] == "METERING", "CREDITS"] = "not-a-number"
    out = _run(bad_num)
    assert out["state"] == "shape" and out["summary"]["missing"] == ["CREDITS (unreadable values)"]
    bad_day = _synthetic_frame()
    bad_day["FIRST_DAY"] = "junk"
    assert _run(bad_day)["state"] == "shape"
    no_cov = _synthetic_frame()
    no_cov = no_cov[~((no_cov["LINE_KIND"] == "COVERAGE") & (no_cov["SERVICE_TYPE"] == "FACT_METERING_DAILY"))]
    assert _run(no_cov)["summary"]["missing"] == ["FACT_METERING_DAILY coverage row"]
    null_company = _synthetic_frame()
    null_company.loc[null_company["LINE_KIND"] == "WAREHOUSE", "COMPANY"] = None
    nulls = _run(null_company)
    assert nulls["state"] == "ok"
    assert _by_company(nulls).loc[showback.UNKNOWN_ROW, "WAREHOUSE_USD"] == pytest.approx(1010.0 * RATE)
    mixed = _synthetic_frame()
    mixed["LINE_KIND"] = mixed["LINE_KIND"].str.lower()                  # case-insensitive kinds
    assert _run(mixed)["state"] == "ok"
    # an unknown tier / service type is priced, never a KeyError
    odd = _synthetic_frame(storage_acct={"TABLE": 1.0, "MYSTERY": 1.0},
                           metering=[("SOMETHING_NEW", 5.0, 0.0), ("WAREHOUSE_METERING", 1000.0, 0.0)])
    assert _run(odd)["state"] == "ok"


def test_coverage_notes_name_late_start_stale_end_and_storage_gaps():
    cov = dict(_COVERAGE)
    cov["FACT_METERING_DAILY"] = ("2025-11-01", "2026-09-25")
    cov["FACT_WAREHOUSE_DAILY"] = ("2025-01-01", "2026-09-10")
    out = _run(_synthetic_frame(window=("2025-09-30", "2026-09-29"),
                                span=("2025-11-01", "2026-09-24", 320),
                                storage_span=("2026-07-01", "2026-09-24", 86), coverage=cov))
    notes = out["notes"]
    assert ("Daily metering starts Nov 1, 2025, so the span starts there, not at the Window's start "
            "(Sep 30, 2025).") in notes
    assert ("The newest daily-metering day is Sep 25, 2026; it is left out as possibly unfinished, so the "
            "span ends Sep 24, 2026.") in notes
    assert ("Daily metering has no row for 8 days inside the span; those days are left out of every "
            "row.") in notes
    assert ("Warehouse: FACT_WAREHOUSE_DAILY's newest row is Sep 10, 2026; any warehouse spend after it "
            "counts as unattributed.") in notes
    assert ("Serverless: FACT_OBJECT_COST_DAILY starts Jun 30, 2026; any serverless spend before then "
            "counts as unattributed.") in notes
    assert ("Cortex Code: FACT_AI_USAGE_DAILY starts Apr 6, 2026; any Cortex Code spend before then counts "
            "as unattributed.") in notes
    assert ("Storage covers 86 of the span's 320 days (Jul 1, 2026 – Sep 24, 2026); storage on the other "
            "days is in neither the company rows nor the all-in total.") in notes
    # the per-database storage line is compared to the storage days, not the whole span
    assert not any(n.startswith("Storage: FACT_STORAGE_DAILY") for n in notes)
    missing = dict(_COVERAGE)
    missing["FACT_OBJECT_COST_DAILY"] = (None, None)
    gone = _run(_synthetic_frame(storage_span=(None, None, 0), coverage=missing))["notes"]
    assert ("Serverless: FACT_OBJECT_COST_DAILY has no rows yet, so this line is empty and any serverless "
            "spend counts as unattributed.") in gone
    assert ("Storage: FACT_STORAGE_ACCOUNT_DAILY has no rows for these days, so storage is in neither the "
            "company rows nor the all-in total.") in gone
    one = _run(_synthetic_frame(span=("2026-09-01", "2026-09-29", 28)))["notes"]
    assert "Daily metering has no row for 1 day inside the span; those days are left out of every row." in one


def test_end_gap_inside_a_bounded_window_is_named_truthfully():
    # Last month, metering runs past the Window but has no rows for its last days
    out = _run(_synthetic_frame(window=("2026-08-01", "2026-08-31"), span=("2026-08-01", "2026-08-25", 25),
                                storage_span=("2026-08-01", "2026-08-25", 25)))
    assert ("Daily metering has no row after Aug 25, 2026 inside the Window, so the span ends there."
            in out["notes"])
    assert not any("left out as possibly unfinished" in n for n in out["notes"])


def test_clean_coverage_emits_no_notes():
    out = _run(_synthetic_frame())
    assert out["notes"] == []
    assert out["summary"]["span_label"] == "Aug 31 – Sep 29"
    assert out["summary"]["span_days"] == 30


def test_span_label_same_year_and_cross_year():
    assert showback.span_label(date(2026, 8, 31), date(2026, 9, 29)) == "Aug 31 – Sep 29"
    assert showback.span_label(date(2025, 12, 1), date(2026, 1, 30)) == "Dec 1, 2025 – Jan 30, 2026"
    assert showback.day_label(date(2026, 6, 30)) == "Jun 30, 2026"


def test_storage_tier_rates_defaults_come_from_config():
    r = showback.storage_tier_rates({})
    assert r["TABLE"] == r["FAILSAFE"] == r["STAGE"] == DEFAULT_STORAGE_USD_PER_TB_MONTH
    for tier, key in (("HYBRID", "STORAGE_HYBRID_USD_PER_TB_MONTH"),
                      ("ARCHIVE_COOL", "STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH"),
                      ("ARCHIVE_COLD", "STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH")):
        assert r[tier] == DEFAULT_SETTINGS[key]
    custom = showback.storage_tier_rates({"STORAGE_USD_PER_TB_MONTH": 40, "STORAGE_HYBRID_USD_PER_TB_MONTH": "x"})
    assert custom["TABLE"] == custom["STAGE"] == custom["FAILSAFE"] == 40.0     # stage defaults to standard
    assert custom["HYBRID"] == DEFAULT_SETTINGS["STORAGE_HYBRID_USD_PER_TB_MONTH"]
    assert set(r) == set(showback.STORAGE_TIERS)


def test_metering_bucket_routes_every_category():
    assert showback.metering_bucket("WAREHOUSE_METERING") == "WAREHOUSE"
    assert showback.metering_bucket("PIPE") == "SERVERLESS"
    assert showback.metering_bucket("AI_SERVICES") == "AI"
    for other in ("WAREHOUSE_METERING_READER", "REPLICATION", "HYBRID_TABLE_REQUESTS", "STORAGE", "NEW_THING"):
        assert showback.metering_bucket(other) == "OTHER", other


def test_attributable_categories_not_widened():
    # the metering lens stays warehouse-only; the showback reads the other facts' keys instead
    assert cost_coverage._ATTRIBUTABLE_CATEGORIES == ("Warehouse",)


# ---------------------------------------------------------------------------
# v4.604.0 review fixes: R1-12 (share basis), R1-13 (partly loaded newest day), R1-14 (keyed gaps)
# ---------------------------------------------------------------------------

def _cs_capped_frame(**kw) -> pd.DataFrame:
    """R1-12's reproduction: cloud services at the 10%-of-compute cap on a CS-heavy account, so the
    company warehouse credits (metering before the adjustment) exceed billed warehouse metering."""
    return _synthetic_frame(
        metering=[("WAREHOUSE_METERING", 4080.0, -420.0), ("SERVERLESS_TASK", 300.0, 0.0),
                  ("AI_SERVICES", 820.0, 0.0)],
        warehouse={"ALFA": 3000.0, "Trexis": 1200.0, "UNKNOWN": 250.0},
        serverless=[("ALFA", "SERVERLESS_TASK", 150.0), ("Trexis", "SERVERLESS_TASK", 100.0)],
        coco=[("ALFA", 500.0), ("Trexis", 220.0)], **kw)


def test_share_never_passes_100_when_the_adjustment_exceeds_the_unattributed_row():
    out = _run(_cs_capped_frame())
    s = out["summary"]
    assert out["state"] == "ok"
    # the scenario: the no-key remainder is smaller than the adjustment credit
    assert 0 < s["unattributed_usd"] < -s["adjustment_usd"]
    assert s["company_usd"] > s["allin_total_usd"]        # company rows / all-in would read 105%
    assert 0 < s["company_share_pct"] <= 100.0
    gross = s["allin_total_usd"] - s["adjustment_usd"]
    assert s["share_basis_usd"] == pytest.approx(gross)
    # "Company-attributed share" is the true complement of "Unattributed (no company key)"
    assert s["company_share_pct"] + s["unattributed_usd"] / gross * 100.0 == pytest.approx(100.0)
    t = _by_company(out)
    assert math.isnan(t.loc[showback.ADJUSTMENT_ROW, "SHARE_OF_TOTAL_PCT"])
    shares = t["SHARE_OF_TOTAL_PCT"].dropna()
    assert shares.sum() == pytest.approx(100.0) and (shares.between(0.0, 100.0)).all()
    # the dollars and the tie-out stay on the billed basis
    assert t["TOTAL_USD"].sum() == pytest.approx(s["allin_total_usd"])
    assert s["company_usd"] + s["adjustment_usd"] + s["unattributed_usd"] == pytest.approx(s["allin_total_usd"])
    assert not out["notes"]                                # every family residual is positive: nothing to say
    # a scoped read of the same account divides by the same basis
    alfa = _run(_cs_capped_frame(only="ALFA"), company="ALFA")
    assert alfa["summary"]["share_basis_usd"] == pytest.approx(gross)
    assert alfa["summary"]["company_share_pct"] == pytest.approx(t.loc["ALFA", "SHARE_OF_TOTAL_PCT"])
    assert alfa["table"].loc[0, "SHARE_OF_TOTAL_PCT"] == pytest.approx(alfa["summary"]["company_share_pct"])


def test_no_share_without_a_positive_basis():
    zero = _run(_synthetic_frame(metering=[("WAREHOUSE_METERING", 0.0, 0.0)], warehouse={}, serverless=[],
                                 coco=[], storage_db={}, storage_span=(None, None, 0)))
    assert zero["state"] == "ok"
    assert zero["summary"]["share_basis_usd"] == 0 and zero["summary"]["company_share_pct"] is None
    assert zero["table"]["SHARE_OF_TOTAL_PCT"].isna().all()


_MORNING_BEFORE = {"FACT_OBJECT_COST_DAILY": "2026-09-29 07:05:00", "FACT_AI_USAGE_DAILY": "2026-09-29 07:40:00"}
_NEXT_MORNING = {"FACT_OBJECT_COST_DAILY": "2026-09-30 07:05:00", "FACT_AI_USAGE_DAILY": "2026-09-30 07:40:00"}


def test_partly_loaded_newest_day_is_named_after_the_metering_load():
    # R1-13: at 06:45 CT on Sep 30 the metering load moves Sep 29 into the span, while the object-cost
    # and Cortex Code facts still hold only the part of Sep 29 they loaded on the morning of Sep 29
    out = _run(_synthetic_frame(loaded=_MORNING_BEFORE))
    assert ("Serverless: FACT_OBJECT_COST_DAILY's rows for Sep 29, 2026 were loaded before that day was "
            "complete, so part of that day's serverless spend counts as unattributed until the loader runs "
            "again.") in out["notes"]
    assert ("Cortex Code: FACT_AI_USAGE_DAILY's rows for Sep 29, 2026 were loaded before that day was "
            "complete, so part of that day's Cortex Code spend counts as unattributed until the loader runs "
            "again.") in out["notes"]
    assert out["keyed_gaps"] == ["FACT_OBJECT_COST_DAILY", "FACT_AI_USAGE_DAILY"]
    # once the loaders have run after the day ended, the day is complete: nothing to say
    done = _run(_synthetic_frame(loaded=_NEXT_MORNING))
    assert done["notes"] == [] and done["keyed_gaps"] == []
    # no load time on the row (the warehouse and storage facts): never a partial note
    assert _run(_synthetic_frame())["notes"] == []


def test_partial_threshold_is_the_day_end_plus_the_view_latency():
    assert showback.LOAD_LATENCY_HOURS == 3
    day = date(2026, 9, 29)
    assert showback.loaded_before_day_complete(datetime(2026, 9, 29, 23, 59), day)
    assert showback.loaded_before_day_complete(datetime(2026, 9, 30, 2, 59), day)
    assert not showback.loaded_before_day_complete(datetime(2026, 9, 30, 3, 0), day)
    late = _run(_synthetic_frame(loaded={"FACT_OBJECT_COST_DAILY": "2026-09-30 02:59:00"}))
    assert late["keyed_gaps"] == ["FACT_OBJECT_COST_DAILY"]
    on_time = _run(_synthetic_frame(loaded={"FACT_OBJECT_COST_DAILY": "2026-09-30 03:00:00"}))
    assert on_time["keyed_gaps"] == [] and on_time["notes"] == []
    # an aware value keeps its wall time (the connector hands LTZ back in the session's Central zone)
    aware = _run(_synthetic_frame(loaded={"FACT_OBJECT_COST_DAILY": "2026-09-29 07:05:00-05:00"}))
    assert aware["keyed_gaps"] == ["FACT_OBJECT_COST_DAILY"]


def test_frozen_loader_newest_day_loaded_in_part_says_on_and_after():
    # the V139-style freeze: the object-cost fact stopped on the morning of Sep 9
    cov = dict(_COVERAGE)
    cov["FACT_OBJECT_COST_DAILY"] = ("2026-06-30", "2026-09-09")
    frozen = _run(_synthetic_frame(coverage=cov, loaded={"FACT_OBJECT_COST_DAILY": "2026-09-09 07:05:00"}))
    assert ("Serverless: FACT_OBJECT_COST_DAILY's newest row is Sep 9, 2026, loaded before that day was "
            "complete; the rest of that day's serverless spend and any after it counts as unattributed."
            in frozen["notes"])
    assert not any("any serverless spend after it" in n for n in frozen["notes"])
    assert frozen["keyed_gaps"] == ["FACT_OBJECT_COST_DAILY"]
    # a freeze whose last day did complete keeps the 'after it' wording
    cleanly = _run(_synthetic_frame(coverage=cov, loaded={"FACT_OBJECT_COST_DAILY": "2026-09-10 07:05:00"}))
    assert ("Serverless: FACT_OBJECT_COST_DAILY's newest row is Sep 9, 2026; any serverless spend after it "
            "counts as unattributed.") in cleanly["notes"]
    assert cleanly["keyed_gaps"] == ["FACT_OBJECT_COST_DAILY"]
    # a newest day before the span is outside it: the partial wording never applies
    cov["FACT_OBJECT_COST_DAILY"] = ("2026-06-30", "2026-07-15")
    before = _run(_synthetic_frame(coverage=cov, loaded={"FACT_OBJECT_COST_DAILY": "2026-07-15 07:05:00"}))
    assert ("Serverless: FACT_OBJECT_COST_DAILY's newest row is Jul 15, 2026; any serverless spend after it "
            "counts as unattributed.") in before["notes"]


def test_unreadable_load_time_is_shape_never_a_clean_read():
    bad = _synthetic_frame()
    bad["LOADED_AT"] = bad["LOADED_AT"].astype(object)
    bad.loc[bad["SERVICE_TYPE"] == "FACT_OBJECT_COST_DAILY", "LOADED_AT"] = "not-a-time"
    out = _run(bad)
    assert out["state"] == "shape" and out["summary"]["missing"] == ["LOADED_AT (unreadable values)"]


def test_keyed_gaps_list_every_source_that_does_not_cover_the_span():
    # R1-14: the list an empty company scope checks before it may read verified-clean
    assert _run(_synthetic_frame())["keyed_gaps"] == []
    cov = dict(_COVERAGE)
    cov["FACT_WAREHOUSE_DAILY"] = (None, None)                       # never loaded
    cov["FACT_OBJECT_COST_DAILY"] = ("2026-06-30", "2026-09-10")     # stale
    cov["FACT_AI_USAGE_DAILY"] = ("2026-09-05", "2026-09-29")        # starts inside the span
    out = _run(_synthetic_frame(coverage=cov))
    assert out["keyed_gaps"] == ["FACT_WAREHOUSE_DAILY", "FACT_OBJECT_COST_DAILY", "FACT_AI_USAGE_DAILY"]
    # the gaps are exactly the lines the notes name
    for table in out["keyed_gaps"]:
        assert any(table in n for n in out["notes"]), table
    # storage on fewer days than the span leaves the per-database storage line unverified too
    short = _run(_synthetic_frame(storage_span=("2026-09-10", "2026-09-29", 20)))
    assert short["keyed_gaps"] == ["FACT_STORAGE_ACCOUNT_DAILY"]
    none = _run(_synthetic_frame(storage_span=(None, None, 0)))
    assert none["keyed_gaps"] == ["FACT_STORAGE_ACCOUNT_DAILY"]
    stale_db = dict(_COVERAGE)
    stale_db["FACT_STORAGE_DAILY"] = ("2025-01-01", "2026-09-20")
    assert _run(_synthetic_frame(coverage=stale_db))["keyed_gaps"] == ["FACT_STORAGE_DAILY"]
    # the same list under a company scope, and an empty list (never a missing key) in the non-ok states
    scoped = _run(_synthetic_frame(only="UNKNOWN", coverage=cov), company="UNKNOWN")
    assert scoped["keyed_gaps"] == out["keyed_gaps"]
    assert _run(None)["keyed_gaps"] == []
    # the public helper agrees: with no coverage rows at all, every keyed line is a gap
    assert showback.keyed_gaps(out["summary"], {}) == [t for t, _, _ in showback._SOURCE_LINES]
