"""Next-Fifty #31: a booked warehouse-setting saving the daily change scan later sees UNDONE leaves the
verified run-rate (the ROI numerator, Added this quarter, the attribution split, the Brief tile, the month /
lever bars, proven-fix transfer) the day the scan sees it; the estimate-accuracy figures keep it.

String, shape and consistency locks on the SQL (the executed arithmetic is in
tests/test_ledger_reverts_harness.py): the registry-only revert CTE, the ONE shared "counts toward the
run-rate" predicate, the revertible settings == the settings SP_LEDGER_AUTOBOOK books, the SIZE ladder ==
sizing.SIZE_ORDER == V153's, and that the retired "reverts are not detected" wording is gone.
"""

from __future__ import annotations

import re
from itertools import pairwise

import pytest

from app import config
from app.config import LEDGER_AUTOBOOKED_LEVERS, LEDGER_REVERTIBLE_SETTINGS, SAVINGS_ACTIVE_MONTHS
from app.core.query import _domains_in
from app.data import mart_sql
from app.data.common import account_today_sql
from app.logic.sizing import SIZE_ORDER
from tests._source import read

_V153 = "snowflake/migrations/V153__ledger_autobook_full_window_settle.sql"
_Q0 = f"DATE_TRUNC('quarter', {account_today_sql()})"
_A0 = f"DATEADD('month', -{int(SAVINGS_ACTIVE_MONTHS)}, {account_today_sql()})"


def _v153_insert_block() -> str:
    sql = read(_V153)
    return sql.split("INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER", 1)[1].split("-- Settle forward-only", 1)[0]


def test_revert_select_shape():
    sqlglot = pytest.importorskip("sqlglot")
    sel = mart_sql._ledger_revert_select()
    sqlglot.parse_one(f"WITH {mart_sql._ledger_revert_cte()}\nSELECT * FROM rv", read="snowflake")
    assert "b.CHANGE_ID AS BOOKED_CHANGE_ID" in sel and "n.CHANGE_ID AS REVERT_CHANGE_ID" in sel
    assert "n.CHANGE_SEEN_AT AS REVERTED_AT" in sel
    for on in ("ON n.WAREHOUSE_NAME = b.WAREHOUSE_NAME", "AND n.SETTING = b.SETTING",
               "AND n.CHANGE_SEEN_AT > b.CHANGE_SEEN_AT"):
        assert on in sel, on
    n_new = mart_sql._setting_cost_rank_sql("n.SETTING", "n.NEW_VALUE")
    # the TRIGGER compares the later change with the booked NEW value (what VERIFIED_USD was measured at) ...
    assert f"AND {n_new} > {mart_sql._setting_cost_rank_sql('b.SETTING', 'b.NEW_VALUE')}" in sel
    # ... the KIND with the booked OLD value (full = back to, or past, where it started)
    assert (f"IFF({n_new} >= {mart_sql._setting_cost_rank_sql('b.SETTING', 'b.OLD_VALUE')}, 'full', 'partial')"
            " AS REVERT_KIND") in sel
    assert sel.rstrip().endswith(
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY b.CHANGE_ID ORDER BY n.CHANGE_SEEN_AT, n.CHANGE_ID) = 1")
    assert "WHERE b.SETTING IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'SCALING_POLICY', 'SIZE')" in sel
    # a pure registry read: no ledger inside (keyed on the booked CHANGE_ID, 1:1), no forbidden tokens
    assert "SAVINGS_LEDGER" not in sel and sel.count("WAREHOUSE_CHANGE_REGISTRY") == 2
    for bad in ("TRY_TO_NUMBER", "CURRENT_DATE()", " LIKE ", "LIMIT", "COMPANY_FOR_WAREHOUSE"):
        assert bad not in sel, bad
    # the b/n aliases never match the LBA-1 settle window lock (tests/test_ledger_twins.py _RN_RE)
    assert "PARTITION BY r." not in sel


def test_revertible_settings_are_exactly_the_autobooked_arms():
    arms = set(re.findall(r"r\.SETTING = '(\w+)'", _v153_insert_block()))
    assert set(LEDGER_REVERTIBLE_SETTINGS) == arms == {"AUTO_SUSPEND", "MAX_CLUSTERS", "SCALING_POLICY", "SIZE"}
    assert "MIN_CLUSTERS" not in LEDGER_REVERTIBLE_SETTINGS          # tracked by the scan, never booked
    # every app lever the autobook books is revert-checked (registry spelling: RESIZE == SIZE)
    assert {("SIZE" if x == "RESIZE" else x) for x in LEDGER_AUTOBOOKED_LEVERS} <= set(LEDGER_REVERTIBLE_SETTINGS)


def _ladder(case_sql: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"WHEN '(\w+)' THEN (\d+)", case_sql)}


def test_size_rank_follows_the_sizing_ladder_and_v153():
    rank = mart_sql._setting_cost_rank_sql("b.SETTING", "b.NEW_VALUE")
    size_case = rank.split("WHEN 'SIZE' THEN ", 1)[1].split(" END", 1)[0]
    ladder = _ladder(size_case)
    assert [ladder[s] for s in SIZE_ORDER] == list(range(1, len(SIZE_ORDER) + 1))     # strictly increasing
    assert ladder["XXLARGE"] == ladder["2XLARGE"]
    assert set(ladder) == {*SIZE_ORDER, "XXLARGE"}
    assert "UPPER(REPLACE(TRIM(COALESCE(b.NEW_VALUE, '')), '-', ''))" in size_case    # X-Large == XLARGE
    # V153's booking ladder (XSMALL..4XLARGE) orders the sizes the same way
    v153 = _ladder(_v153_insert_block().split("r.SETTING = 'SIZE'", 2)[2].split(" ELSE ", 1)[0])
    shared = sorted(v153, key=v153.__getitem__)
    assert shared == sorted(shared, key=ladder.__getitem__) and len(shared) >= 9
    assert all(v153[a] == v153[b] or ladder[a] < ladder[b] for a, b in pairwise(shared))


def test_other_setting_ranks():
    rank = mart_sql._setting_cost_rank_sql("n.SETTING", "n.NEW_VALUE")
    # NULL / <= 0 auto-suspend never suspends: the COSTLIEST value, so 60 -> NULL / 0 is a revert
    assert ("WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(n.NEW_VALUE), 0) <= 0, 1000000000000, "
            "TRY_TO_DOUBLE(n.NEW_VALUE))") in rank
    assert "WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(n.NEW_VALUE)" in rank
    assert ("WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(n.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 "
            "WHEN 'STANDARD' THEN 2 END") in rank
    assert "MIN_CLUSTERS" not in rank                                   # unknown setting -> NULL -> fail open


def test_one_counts_predicate_everywhere():
    summary = mart_sql.savings_summary_quarter()
    for anchor in (_Q0, _A0):
        pred = mart_sql._ledger_counts_predicate(anchor)
        assert summary.count(pred) == 2, anchor                  # the $ and the item count
    assert mart_sql._ledger_counts_predicate(_A0) in mart_sql.ledger_attribution()
    pred = mart_sql._ledger_counts_predicate(_A0)
    # the revert clause stays LAST, so the pre-#31 prefix is still a substring (test_proof_evidence)
    assert pred.startswith(f"l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL AND l.VERIFIED_AT >= {_A0}")
    assert pred.endswith(" AND rv.REVERTED_AT IS NULL")


def test_every_ledger_read_joins_the_revert_cte_one_to_one():
    link = "LEFT JOIN rv ON rv.BOOKED_CHANGE_ID = l.SOURCE_CHANGE_ID"
    for name, sql in (("summary", mart_sql.savings_summary_quarter()),
                      ("attribution", mart_sql.ledger_attribution()),
                      ("ledger", mart_sql.savings_ledger(limit=None)),
                      ("ledger_capped", mart_sql.savings_ledger()),
                      ("wins", mart_sql.verified_wins("ALL")),
                      ("wins_trexis", mart_sql.verified_wins("Trexis"))):
        assert sql.count(link) == 1, name
        assert sql.count(mart_sql._ledger_revert_cte()) == 1, name
    wins = mart_sql.verified_wins("ALL")
    assert "AND t.TWIN_ITEM_ID IS NULL\n  AND rv.REVERTED_AT IS NULL" in wins     # a reverted fix is not proven
    # history reads never apply it: the funnel counts events, the verifier covers manual rows only
    assert "rv." not in mart_sql.acceptance_funnel(90)
    assert "rv." not in mart_sql.savings_verification_runs()


def test_savings_ledger_appends_exactly_the_revert_columns():
    sqlglot = pytest.importorskip("sqlglot")
    for limit in (None, 500):
        cols = sqlglot.parse_one(mart_sql.savings_ledger(limit=limit), read="snowflake").named_selects
        assert cols[-6:] == ["WINDOW_CLOSED", "REVERTED_AT", "REVERT_CHANGE_ID", "REVERT_OLD_VALUE",
                             "REVERT_NEW_VALUE", "REVERT_KIND"]


def test_cache_domains_unchanged():
    # the registry is in no cache domain, so the new joins never re-cold these reads on another write
    # (the ledger's "settings" is its pre-#31 credit-rate CROSS JOIN)
    assert set(_domains_in(mart_sql.savings_summary_quarter())) == {"ledger"}
    assert set(_domains_in(mart_sql.savings_ledger(limit=None))) == {"ledger", "settings"}
    assert set(_domains_in(mart_sql.verified_wins("ALL"))) == {"ledger"}
    assert _domains_in(mart_sql._ledger_revert_select()) == []


def test_the_stand_in_wording_is_gone():
    assert "Reverted changes are not detected yet" not in read("app/ui/decision_studio.py")
    assert "Reverts are not detected yet" not in read("app/data/mart_sql.py")
    assert "revert detection is not built yet" not in read("app/config.py")
    assert config.SAVINGS_ACTIVE_MONTHS == 12                      # the cap stays for app-booked rows
    ds = read("app/ui/decision_studio.py")
    assert "leaves the run-rate the day the scan sees it; everything else counts" in ds
    brief = read("app/ui/pages/brief.py")
    assert "(excluding warehouse changes the daily scan later saw undone)" in brief
    assert "/mo added this quarter" in brief
