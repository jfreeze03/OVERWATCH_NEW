"""Next-Fifty #31: Proof ▸ Proof "Saved to date" — how much the verified savings have saved so far, in
DOLLARS (the run-rate KPI explicitly declines to answer that). Four aggregate columns ride the summary Proof
already batch-reads (zero new reads); the card discloses measured vs carried-forward, is chipped "accrued",
never reads "/mo" / "(all time)" / "(YTD)" (the D4 lesson), and never feeds the ROI multiple, the verdict or
the Brief. The executed accrual arithmetic is in tests/test_ledger_reverts_harness.py.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd
import pytest

import app.logic.actions as actions_mod
import app.ui.decision_studio as ds
from app import config
from app.config import SAVINGS_ACTIVE_MONTHS
from app.core.result import QueryResult
from app.data import mart_sql
from app.data.common import account_today_sql
from app.logic import proof
from tests._source import page_source, read

_TODAY = account_today_sql()
# savings_summary_quarter's first six aggregates as they stood BEFORE Next-Fifty #31, whitespace-normalized
# (hard-coded, never derived from the builder): #31 may only append the revert clause to them.
_Q0 = "DATE_TRUNC('quarter', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)"
_A0 = "DATEADD('month', -12, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)"
_PRE_31 = {
    "VERIFIED_QTD_USD": ("ROUND(SUM(IFF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL AND l.VERIFIED_AT >= "
                         f"{_Q0}, COALESCE(l.VERIFIED_USD, 0), 0)), 2)"),
    "VERIFIED_ITEMS": f"COUNT_IF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL AND l.VERIFIED_AT >= {_Q0})",
    "VERIFIED_ACTIVE_MONTHLY_USD": ("ROUND(SUM(IFF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL AND "
                                    f"l.VERIFIED_AT >= {_A0}, COALESCE(l.VERIFIED_USD, 0), 0)), 2)"),
    "VERIFIED_ACTIVE_ITEMS": f"COUNT_IF(l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL AND l.VERIFIED_AT >= {_A0})",
    "ESTIMATED_OPEN_USD": ("ROUND(SUM(IFF(l.STATE = 'ESTIMATED' AND t.TWIN_ITEM_ID IS NULL, "
                           "COALESCE(l.ESTIMATED_USD, 0), 0)), 2)"),
    "SUPERSEDED_ITEMS": "COUNT_IF(t.TWIN_ITEM_ID IS NOT NULL AND l.STATE <> 'REJECTED')",
}
_REV = " AND rv.REVERTED_AT IS NULL"


def _projections(sql: str) -> dict[str, str]:
    """alias -> whitespace-normalized expression of the main SELECT (one aggregate per 4-space line)."""
    body = sql.rsplit("\nSELECT\n", 1)[1].split("\nFROM ", 1)[0]
    out: dict[str, str] = {}
    for item in re.split(r",\n    (?=[A-Z])", body.strip()):
        expr, alias = item.rsplit(" AS ", 1)
        out[alias.strip()] = " ".join(expr.split())
    return out


def _fn(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


# --------------------------------------------------------------------------- the builder
def test_summary_builder_contract():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.savings_summary_quarter()
    cols = sqlglot.parse_one(sql, read="snowflake").named_selects
    assert cols == [*_PRE_31, "REVERTED_ACTIVE_ITEMS", "REVERTED_ACTIVE_USD", *proof.SAVED_TO_DATE_COLUMNS]
    assert _TODAY == "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE"
    assert "CURRENT_DATE()" not in sql and "LIMIT" not in sql
    proj = _projections(sql)
    assert list(proj) == cols
    start = "COALESCE(r.CHANGE_SEEN_AT::TIMESTAMP_NTZ, l.VERIFIED_AT)::DATE"
    stop = (f"LEAST({_TODAY}, DATEADD('month', {SAVINGS_ACTIVE_MONTHS}, l.VERIFIED_AT::DATE), "
            f"COALESCE(rv.REVERTED_AT::TIMESTAMP_NTZ::DATE, {_TODAY}))")      # LEAST(.., NULL) would be NULL
    days = f"COALESCE(GREATEST(0, DATEDIFF('day', {start}, {stop})), 0)"
    per_day = "COALESCE(l.VERIFIED_USD, 0) / 30.0"
    live = "l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL"
    total = proj["SAVED_TO_DATE_USD"]
    assert total == f"ROUND(SUM(IFF({live}, {per_day} * {days}, 0)), 2)"
    assert "rv.REVERTED_AT IS NULL" not in total                # reverted rows accrue up to the revert
    mdays = ("COALESCE(IFF(r.CHANGE_ID IS NULL, 0, GREATEST(0, LEAST(COALESCE(r.AFTER_DAYS, 0), "
             f"DATEDIFF('day', {start}, l.VERIFIED_AT::DATE)))), 0)")
    assert proj["SAVED_MEASURED_USD"] == f"ROUND(SUM(IFF({live}, {per_day} * LEAST({days}, {mdays}), 0)), 2)"
    assert proj["SAVED_BEFORE_REVERT_USD"] == (
        f"ROUND(SUM(IFF({live} AND rv.REVERTED_AT IS NOT NULL, {per_day} * {days}, 0)), 2)")
    assert proj["SAVED_SINCE_DATE"] == f"MIN(IFF({live} AND COALESCE(l.VERIFIED_USD, 0) > 0, {start}, NULL))"
    rev_active = f"{live} AND l.VERIFIED_AT >= {_A0} AND rv.REVERTED_AT IS NOT NULL"
    assert proj["REVERTED_ACTIVE_ITEMS"] == f"COUNT_IF({rev_active})"
    assert proj["REVERTED_ACTIVE_USD"] == f"ROUND(SUM(IFF({rev_active}, COALESCE(l.VERIFIED_USD, 0), 0)), 2)"
    # both joins 1:1: the registry by its CHANGE_ID key, the revert CTE by its QUALIFY
    assert "LEFT JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r ON r.CHANGE_ID = l.SOURCE_CHANGE_ID" in sql
    assert "LEFT JOIN rv ON rv.BOOKED_CHANGE_ID = l.SOURCE_CHANGE_ID" in sql


def test_the_pre_31_aggregates_only_gain_the_revert_clause():
    proj = _projections(mart_sql.savings_summary_quarter())
    for alias, before in _PRE_31.items():
        now = proj[alias]
        assert now.replace(_REV, "") == before, alias
        assert now.count(_REV) == (0 if alias == "SUPERSEDED_ITEMS" else 1), alias


def test_the_monthly_normalisation_matches_the_settle():
    assert config.SAVINGS_MONTH_DAYS == 30
    assert "/ 30.0" in _projections(mart_sql.savings_summary_quarter())["SAVED_TO_DATE_USD"]
    # V153 settles VERIFIED_USD as (BASE - AFTER) credits/day x rate x 30: a 30-day month
    assert "* :rate * 30 AS WH_SAVED_MONTHLY_USD" in read(
        "snowflake/migrations/V153__ledger_autobook_full_window_settle.sql")


# --------------------------------------------------------------------------- the reader
def _summary(**over) -> pd.DataFrame:
    row = {"VERIFIED_ACTIVE_MONTHLY_USD": 450.0, "SAVED_TO_DATE_USD": 965.0, "SAVED_MEASURED_USD": 219.01,
           "SAVED_BEFORE_REVERT_USD": 155.0, "SAVED_SINCE_DATE": "2026-07-20", **over}
    return pd.DataFrame([row])


def test_reader_worked_example_and_shapes():
    assert proof.saved_to_date(_summary()) == {"total_usd": 965.0, "measured_usd": 219.01, "carried_usd": 745.99,
                                               "before_revert_usd": 155.0, "since": date(2026, 7, 20)}
    assert proof.saved_to_date(None) is None and proof.saved_to_date(pd.DataFrame()) is None
    # an older-shaped summary (no SAVED_* columns): None, never a fallback
    assert proof.saved_to_date(pd.DataFrame([{"VERIFIED_ACTIVE_MONTHLY_USD": 1.0}])) is None
    assert proof.saved_to_date(_summary().drop(columns=["SAVED_SINCE_DATE"])) is None
    # rounding can leave measured a cent above the total: clamped, never a negative carry
    clamp = proof.saved_to_date(_summary(SAVED_TO_DATE_USD=100.0, SAVED_MEASURED_USD=100.01))
    assert (clamp["measured_usd"], clamp["carried_usd"]) == (100.0, 0.0)
    # NULL aggregates (an empty ledger) read as zero
    null = proof.saved_to_date(_summary(SAVED_TO_DATE_USD=None, SAVED_MEASURED_USD=None,
                                        SAVED_BEFORE_REVERT_USD=None, SAVED_SINCE_DATE=None))
    assert null == {"total_usd": 0.0, "measured_usd": 0.0, "carried_usd": 0.0, "before_revert_usd": 0.0,
                    "since": None}


@pytest.mark.parametrize(("since", "expected"), [
    ("2026-07-20", date(2026, 7, 20)),
    (pd.Timestamp("2026-07-20"), date(2026, 7, 20)),
    (pd.Timestamp("2026-07-20 00:00", tz="America/Chicago"), date(2026, 7, 20)),
    (date(2026, 7, 20), date(2026, 7, 20)),
    (datetime(2026, 7, 20, 6, 40), date(2026, 7, 20)),
    (pd.NaT, None), (None, None), (float("nan"), None), (1.0, None), ("", None), ("not a date", None),
])
def test_reader_since_parses_or_returns_none(since, expected):
    frame = _summary()
    frame["SAVED_SINCE_DATE"] = pd.Series([since], dtype="object")
    assert proof.saved_to_date(frame)["since"] == expected


def test_card_text_is_dollars_with_the_split():
    value, delta = proof.saved_to_date_card(proof.saved_to_date(_summary()), verified_any=True)
    assert value == "$965.00"
    assert delta == ("since Jul 20, 2026 · $219.01 measured · $745.99 carried forward at the verified rate"
                     " · incl. $155.00 saved before a change was undone")
    value, delta = proof.saved_to_date_card(proof.saved_to_date(_summary(SAVED_BEFORE_REVERT_USD=0.0)),
                                            verified_any=True)
    assert "undone" not in delta and delta.endswith("carried forward at the verified rate")
    for verified_any in (True, False):
        assert proof.saved_to_date_card(None, verified_any=verified_any) == (
            "—", "whole-ledger summary unavailable")
    assert proof.saved_to_date_card(proof.saved_to_date(_summary(SAVED_TO_DATE_USD=0.0)), verified_any=False) == (
        "$0.00", "nothing verified yet")
    no_since = proof.saved_to_date_card(proof.saved_to_date(_summary(SAVED_SINCE_DATE=None)),
                                        verified_any=True)[1]
    assert no_since.startswith("$219.01 measured")
    for saved in (None, proof.saved_to_date(_summary()), proof.saved_to_date(_summary(SAVED_TO_DATE_USD=0.0))):
        for verified_any in (True, False):
            v, d = proof.saved_to_date_card(saved, verified_any=verified_any)
            # never a run-rate, never "(all time)" / "(YTD)" (the D4 lesson)
            for bad in ("/mo", "all time", "YTD"):
                assert bad not in v and bad not in d, bad


@pytest.mark.parametrize("zero", [
    # (a) the only verified item was verified by hand TODAY: 0 whole days in effect, so $0 accrued
    {"SAVED_TO_DATE_USD": 0.0, "SAVED_MEASURED_USD": 0.0, "SAVED_BEFORE_REVERT_USD": 0.0,
     "SAVED_SINCE_DATE": "2026-09-28"},
    # (b) every live verified item carries $0 (an LBA-1 partner, a hand-verified "saved nothing")
    {"SAVED_TO_DATE_USD": 0.0, "SAVED_MEASURED_USD": 0.0, "SAVED_BEFORE_REVERT_USD": 0.0,
     "SAVED_SINCE_DATE": None},
])
def test_a_zero_total_beside_verified_items_never_reads_nothing_verified(zero):
    # review r1 F2/F6/F21: the run-rate card beside this one already counts these items, so a $0 accrual is a
    # neutral zero state -- "nothing verified yet" is only for a record with no verified item at all
    value, delta = proof.saved_to_date_card(proof.saved_to_date(_summary(**zero)), verified_any=True)
    assert value == "$0.00"
    assert delta == proof.SAVED_NOTHING_ACCRUED
    assert "nothing verified" not in delta and delta.startswith("nothing accrued yet")
    assert "day after it is verified or its change is seen" in delta
    # the same $0 with no verified item at all is the genuinely empty record
    assert proof.saved_to_date_card(proof.saved_to_date(_summary(**zero)), verified_any=False) == (
        "$0.00", "nothing verified yet")


def test_saved_to_date_card_requires_the_verified_flag():
    # keyword-only and required: a caller can never fall back to "nothing verified yet" by omission
    with pytest.raises(TypeError):
        proof.saved_to_date_card(proof.saved_to_date(_summary()))            # type: ignore[call-arg]


# --------------------------------------------------------------------------- the wiring
def _qr(df: pd.DataFrame, ok: bool = True) -> QueryResult:
    return QueryResult(df=df, ok=ok, source="t", error="" if ok else "boom")


def _signals(monkeypatch, sc_quarter: QueryResult) -> dict:
    ds.reset_proof_memo()
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 28, 9, 0))
    batch = {"sc_quarter": sc_quarter, "sc_appcost": _qr(pd.DataFrame({"APP_CREDITS_30D": [10.0]})),
             "sc_accept": _qr(pd.DataFrame())}
    ledger = pd.DataFrame([{"STATE": "VERIFIED", "ESTIMATED_USD": 600, "VERIFIED_USD": 500,
                            "CREATED_AT": "2026-08-01", "VERIFIED_AT": "2026-09-01"}])
    monkeypatch.setattr(ds, "run_batch", lambda specs, **_k: batch)
    monkeypatch.setattr(ds, "run", lambda sql, **k: (_qr(ledger) if k.get("key") == "decision_roi_ledger_full"
                                                     else sc_quarter if k.get("key") == "sc_quarter"
                                                     else _qr(pd.DataFrame())))
    try:
        return ds._proof_signals(3.68)
    finally:
        ds.reset_proof_memo()


def test_proof_signals_carry_saved_to_date_from_the_summary_only(monkeypatch):
    full = _summary(VERIFIED_QTD_USD=0.0, VERIFIED_ITEMS=0, VERIFIED_ACTIVE_ITEMS=3, ESTIMATED_OPEN_USD=0.0,
                    SUPERSEDED_ITEMS=0, REVERTED_ACTIVE_ITEMS=2, REVERTED_ACTIVE_USD=150.0)
    sig = _signals(monkeypatch, _qr(full))
    assert sig["saved"]["total_usd"] == 965.0 and sig["saved"]["carried_usd"] == 745.99
    assert (sig["reverted_active_items"], sig["reverted_active_usd"]) == (2, 150.0)
    # the ROI numerator is still the active run-rate, never the accrued total
    assert sig["roi"]["VERIFIED_USD"] == 450.0 and sig["verified_active"] == 450.0
    # a failed summary read: NO Saved to date (never a sum over the capped ledger frame), even though the
    # ledger frame holds a verified row; the run-rate falls back to the frame as before (disclosed)
    failed = _signals(monkeypatch, _qr(pd.DataFrame(), ok=False))
    assert failed["saved"] is None and failed["summary_ok"] is False
    assert failed["roi"]["VERIFIED_USD"] == 500.0
    assert (failed["reverted_active_items"], failed["reverted_active_usd"]) == (0, 0.0)
    # an older-shaped summary (the pre-#31 batch fixture): the card reads "—", nothing raises
    old = _signals(monkeypatch, _qr(pd.DataFrame({"VERIFIED_QTD_USD": [0.0], "VERIFIED_ITEMS": [0],
                                                  "VERIFIED_ACTIVE_MONTHLY_USD": [500.0],
                                                  "VERIFIED_ACTIVE_ITEMS": [3], "ESTIMATED_OPEN_USD": [0.0]})))
    assert old["saved"] is None and old["roi"]["VERIFIED_USD"] == 500.0 and old["reverted_active_items"] == 0


def test_proof_page_source_locks():
    src = read("app/ui/decision_studio.py")
    tab = _fn(src, "_proof_tab")
    assert '{"label": "Saved to date", "value": _saved_value,' in tab and '"method": "accrued"' in tab
    # the zero state keys on whether ANY item is verified (reverted ones included), never on the $ alone
    assert ('_saved_value, _saved_delta = saved_to_date_card(\n'
            '        sig.get("saved"), verified_any=_ver_any > 0 or int(sig.get("verified_active_items") or 0) > 0)'
            ) in tab
    assert tab.index("_ver_any = ") < tab.index("saved_to_date_card(")
    assert "_SAVED_TO_DATE_HELP" in tab and "does not feed the ROI multiple" in ds._SAVED_TO_DATE_HELP
    assert "not a run-rate" in ds._SAVED_TO_DATE_HELP
    # row 1: run-rate · Saved to date · Added this quarter · Realization; row 2 starts with Settling
    order = ['"label": "Verified savings run-rate"', '"label": "Saved to date"', '"label": "Added this quarter"',
             '"label": "Realization rate"', "kpi_row([", '"label": "Settling"', '"label": "Acted on"']
    first_row = tab.index("kpi_row([")
    idx = [tab.index(o, first_row + 1) if o == "kpi_row([" else tab.index(o) for o in order]
    assert idx == sorted(idx), idx
    assert "(YTD)" not in src and "Saved to date (all time)" not in src
    # nothing new feeds the ROI multiple, the verdict or the Brief
    for call in re.findall(r"(?:roi_multiple|proof_verdict)\(([^)]*)\)", src):
        assert "saved" not in call, call
    assert "saved" not in _fn(src, "decision_verdict")
    brief = page_source("brief")
    assert "Saved to date" not in brief and "SAVED_TO_DATE_USD" not in brief and "SAVED_" not in brief
    # the all-reverted branch: after "verified items exist, none active", before the empty state
    assert (tab.index('elif int(totals["verified_count"]) > 0:')
            < tab.index('elif int(totals.get("reverted_count") or 0) > 0:')
            < tab.index("No savings verified yet"))
    # the Reverted savings list lives in the evidence region, which never sums the capped frame
    evidence = tab.split("# ---- What each saving rests on", 1)[1].split("month_df =", 1)[0]
    assert "_rev = reverted_rows(ledger.df)" in evidence and ".sum(" not in evidence
    # review r1 F7: the list reads the row-capped frame -- its title says so when truncated -- and the SQL
    # caption only points at it when it is there (computed before the caption)
    assert ('_rev_title = (f"Reverted savings — {len(_rev):,} change(s) undone"\n'
            '                      + (" (newest ledger rows)" if ledger.truncated else ""))') in evidence
    assert "with st.expander(_rev_title):" in evidence
    assert '(" — see Reverted savings below." if not _rev.empty else ".")' in evidence
    assert evidence.index("_rev = reverted_rows(ledger.df)") < evidence.index("if _rev_n:")
    # the run-rate delta's reverted count is the UNCAPPED SQL figure (sig), never the capped frame's
    assert '_rev_n = int(sig.get("reverted_active_items") or 0)' in tab
    assert tab.index('_rev_n = int(sig.get("reverted_active_items") or 0)') < tab.index("kpi_row([")
    assert 'f" · {_rev_n:,} reverted, not counted" if _rev_n else ""' in tab
    assert "totals.get('reverted_count') or 0):,} reverted, not counted" not in tab
    # review r1 F3/F5: the "older items" branch names the in-window items that were undone, never the
    # age-only reason while _rev_n > 0 (the branch ORDER above is unchanged)
    older = tab.split('elif int(totals["verified_count"]) > 0:', 1)[1].split(
        'elif int(totals.get("reverted_count") or 0) > 0:', 1)[0]
    age_only = "verified item(s), none verified in the last "
    assert older.count(age_only) == 1
    assert older.index("if _rev_n > 0:") < older.index("was later undone") < older.index("else:") < older.index(
        age_only)
    # "nothing verified yet" keys on verified OR reverted items (realization keeps reverted rows)
    assert tab.count("if _ver_any else") == 2 and 'if totals["verified_count"] else "nothing' not in tab
