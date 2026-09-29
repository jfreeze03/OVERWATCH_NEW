"""Next-Fifty #27 — a flagged spend day explained BELOW the warehouse (pure logic).

``anomaly_explain.explain_below_warehouse`` splits ONE warehouse's flagged-day move by user and by
database over mart27_sql.alloc_xdim_day_drivers' long frame, against a ZERO-FILLED MEAN over the
loaded spine; ``changes_near_day`` lists that warehouse's setting changes around the day. Locks:
  * the sporadic heavy user the waterfall's row-tail median hides (why the sibling exists);
  * exact additivity — each table (incl. 'All other' and the not-allocated residual) sums to the
    warehouse's metered delta, and the residual is metered minus allocated;
  * zero-fill semantics (partial / new / silent keys; METERED off the spine ignored);
  * the refusals, the top-N fold, the company masks (and the narrative leading with one that
    carries the largest change), share suppression, the narrative;
  * changes_near_day's exact-name match and its account-local ±1-day window.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from app.companies import COMPANIES
from app.logic.anomaly_explain import (
    SQL_OTHER_LABELS,
    UNALLOCATED_LABEL,
    UNCLASSIFIED_USERS_LABEL,
    changes_near_day,
    explain_below_warehouse,
    explain_by_warehouse,
    is_bucket_row,
    outside_company_label,
)

_D = date(2026, 9, 20)


def _rows(day: date, dim: str, key: str, usd: float) -> dict:
    return {"DAY": day, "DIMENSION": dim, "KEY_NAME": key, "USD": usd}


def _sporadic(*, baseline_days: int = 14, idle_base: float = 20.0, idle_d: float = 60.0,
              spine_gap: int | None = None) -> pd.DataFrame:
    """User A $100 on even offsets (and D), B on odd offsets, C $300 on offsets 3/7/11 and D.
    A/B query DB_SALES, C queries DB_ADHOC. METERED = allocated + idle ($20/day, $60 on D).
    ``spine_gap`` drops that offset from the SPINE (the XDIM loader skipped it) — its rows remain
    in the frame, exactly as the reader would still return the METERED/USER rows of a partial day."""
    out: list[dict] = []
    for o in range(baseline_days, -1, -1):
        d = _D - timedelta(days=o)
        if o != spine_gap:
            out.append(_rows(d, "SPINE", "", 0.0))
        alloc = 0.0
        who = "A" if o % 2 == 0 else "B"
        out += [_rows(d, "USER", who, 100.0), _rows(d, "DATABASE", "DB_SALES", 100.0)]
        alloc += 100.0
        if o in (0, 3, 7, 11):
            out += [_rows(d, "USER", "C", 300.0), _rows(d, "DATABASE", "DB_ADHOC", 300.0)]
            alloc += 300.0
        out.append(_rows(d, "METERED", "WH_A", alloc + (idle_d if o == 0 else idle_base)))
    return pd.DataFrame(out)


def _by(rows, name):
    return next(r for r in rows if r.name == name)


# ---------------------------------------------------------------------------
# Why a sibling: the sporadic heavy user
# ---------------------------------------------------------------------------

def test_sporadic_heavy_user_is_the_top_driver():
    exp = explain_below_warehouse(_sporadic(), _D, "WH_A")
    assert exp.ok and exp.baseline_days == 14
    top = exp.by_user[0]
    assert top.name == "C" and top.delta_usd == 235.71
    assert top.baseline_usd == 64.29 and top.actual_usd == 300.0   # 3 x $300 / 14 loaded days
    assert exp.by_database[0].name == "DB_ADHOC" and exp.by_database[0].delta_usd == 235.71


def test_the_waterfalls_row_tail_median_hides_that_same_user():
    """Documents why explain_by_warehouse is NOT reused one level down: C's median over its own three
    sparse rows is $300, so the flagged $300 reads as 'usual' and C drops out of the drivers."""
    users = _sporadic()
    users = users[users["DIMENSION"] == "USER"].rename(columns={"KEY_NAME": "WAREHOUSE_NAME"})
    wf = explain_by_warehouse(users, _D)
    assert all(d.name != "C" for d in wf.drivers)


# ---------------------------------------------------------------------------
# Additivity
# ---------------------------------------------------------------------------

def test_each_table_sums_exactly_to_the_metered_delta():
    exp = explain_below_warehouse(_sporadic(), _D, "WH_A")
    assert exp.metered_actual_usd == 460.0 and exp.metered_baseline_usd == 184.29
    assert exp.metered_delta_usd == 275.71
    for table in (exp.by_user, exp.by_database):
        assert abs(sum(r.delta_usd for r in table) - exp.metered_delta_usd) < 0.01
        assert abs(sum(r.actual_usd for r in table) - exp.metered_actual_usd) < 0.01
        assert abs(sum(r.baseline_usd for r in table) - exp.metered_baseline_usd) < 0.01
        assert table[-1].name == UNALLOCATED_LABEL
    # the residual is metered minus allocated: idle rose $20 -> $60
    assert exp.unallocated_delta_usd == 40.0
    assert _by(exp.by_user, UNALLOCATED_LABEL).delta_usd == 40.0
    assert _by(exp.by_database, UNALLOCATED_LABEL).delta_usd == 40.0


def test_rounding_is_balanced_by_the_residual_row_to_the_cent():
    # thirds everywhere: every row rounds, the table still adds up to the displayed metered move
    rows = []
    for o in range(14, -1, -1):
        d = _D - timedelta(days=o)
        rows += [_rows(d, "SPINE", "", 0), _rows(d, "USER", "U1", 10 / 3), _rows(d, "USER", "U2", 20 / 3),
                 _rows(d, "DATABASE", "DB1", 10.0), _rows(d, "METERED", "WH_A", 10.0 + 1 / 3 + (o == 0) * 7 / 3)]
    exp = explain_below_warehouse(pd.DataFrame(rows), _D, "WH_A")
    for table in (exp.by_user, exp.by_database):
        assert round(sum(r.delta_usd for r in table), 2) == exp.metered_delta_usd
        assert round(sum(r.actual_usd for r in table), 2) == exp.metered_actual_usd


# ---------------------------------------------------------------------------
# Zero-fill semantics
# ---------------------------------------------------------------------------

def test_zero_fill_new_silent_and_partial_keys():
    f = _sporadic()
    f = pd.concat([f, pd.DataFrame([_rows(_D, "USER", "NEWBIE", 42.0), _rows(_D, "DATABASE", "DB_NEW", 42.0),
                                  _rows(_D, "METERED", "WH_A", 42.0)])])
    exp = explain_below_warehouse(f, _D, "WH_A", max_rows=10)
    new = _by(exp.by_user, "NEWBIE")
    assert new.baseline_usd == 0.0 and new.delta_usd == 42.0          # no history: all of it is new
    silent = _by(exp.by_user, "B")                                    # present on 7 baseline days, silent on D
    assert silent.baseline_usd == 50.0 and silent.actual_usd == 0.0 and silent.delta_usd == -50.0
    partial = _by(exp.by_user, "A")                                   # 7 of 14 days: 700 / 14
    assert partial.baseline_usd == 50.0 and partial.delta_usd == 50.0
    assert _by(exp.by_database, "DB_NEW").delta_usd == 42.0
    assert exp.unallocated_delta_usd == 40.0                          # the newcomer was fully allocated


def test_metered_off_the_spine_is_ignored_and_a_skipped_day_shrinks_the_average():
    base = explain_below_warehouse(_sporadic(), _D, "WH_A")
    off = pd.concat([_sporadic(), pd.DataFrame([_rows(_D - timedelta(days=40), "METERED", "WH_A", 9999.0),
                                                 _rows(_D + timedelta(days=1), "METERED", "WH_A", 9999.0)])])
    assert explain_below_warehouse(off, _D, "WH_A").metered_baseline_usd == base.metered_baseline_usd
    # offset 3 (a C day) not loaded by the XDIM loader: excluded from EVERY average, not a zero
    gap = explain_below_warehouse(_sporadic(spine_gap=3), _D, "WH_A")
    assert gap.ok and gap.baseline_days == 13
    assert _by(gap.by_user, "C").baseline_usd == round(600 / 13, 2)
    assert abs(sum(r.delta_usd for r in gap.by_user) - gap.metered_delta_usd) < 0.01


def test_baseline_takes_only_the_last_n_spine_days():
    exp = explain_below_warehouse(_sporadic(baseline_days=20), _D, "WH_A", baseline_days=14)
    assert exp.baseline_days == 14
    assert _by(exp.by_user, "C").baseline_usd == 64.29


# ---------------------------------------------------------------------------
# Refusals — an honest reason, never a wrong split
# ---------------------------------------------------------------------------

def test_refuses_with_fewer_than_seven_baseline_days():
    exp = explain_below_warehouse(_sporadic(baseline_days=5), _D, "WH_A")
    assert not exp.ok and "baseline day" in exp.reason and exp.by_user == ()


def test_refuses_when_the_flagged_day_is_not_loaded():
    f = _sporadic()
    f = f[~((f["DIMENSION"] == "SPINE") & (f["DAY"] == _D))]
    exp = explain_below_warehouse(f, _D, "WH_A")
    assert not exp.ok and "has not loaded" in exp.reason


def test_refuses_without_query_allocated_rows():
    f = _sporadic()
    f = f[f["DIMENSION"].isin(["SPINE", "METERED"])]
    exp = explain_below_warehouse(f, _D, "WH_CS_ONLY")
    assert not exp.ok and "No query-allocated rows for WH_CS_ONLY" in exp.reason


def test_refuses_when_the_day_has_no_metered_row_in_this_scope():
    f = _sporadic()
    f = f[~((f["DIMENSION"] == "METERED") & (f["DAY"] == _D))]
    exp = explain_below_warehouse(f, _D, "WH_A")
    assert not exp.ok and "no metered credits" in exp.reason


def test_refuses_empty_or_missing_inputs():
    assert not explain_below_warehouse(pd.DataFrame(), _D, "WH_A").ok
    assert not explain_below_warehouse(None, _D, "WH_A").ok
    assert not explain_below_warehouse(_sporadic(), None, "WH_A").ok
    assert not explain_below_warehouse(_sporadic().drop(columns=["USD"]), _D, "WH_A").ok


# ---------------------------------------------------------------------------
# Top-N fold, the company masks, share
# ---------------------------------------------------------------------------

def _many_users(extra: list[dict] | None = None) -> pd.DataFrame:
    rows = []
    for o in range(14, -1, -1):
        d = _D - timedelta(days=o)
        rows.append(_rows(d, "SPINE", "", 0))
        total = 0.0
        for i in range(10):
            usd = 10.0 + (i * 5.0 if o == 0 else 0.0)                # U9 moved most, U0 not at all
            rows += [_rows(d, "USER", f"U{i}", usd), _rows(d, "DATABASE", f"DB{i}", usd)]
            total += usd
        rows.append(_rows(d, "METERED", "WH_A", total))
    return pd.concat([pd.DataFrame(rows), pd.DataFrame(extra or [])], ignore_index=True)


def test_top_n_folds_the_rest_and_the_sql_bucket_into_all_other():
    extra = [_rows(_D, "USER", "(all other users)", 7.0), _rows(_D, "DATABASE", "(all other databases)", 7.0),
             _rows(_D, "METERED", "WH_A", 7.0)]
    exp = explain_below_warehouse(_many_users(extra), _D, "WH_A", max_rows=3)
    names = [r.name for r in exp.by_user]
    assert names[:3] == ["U9", "U8", "U7"]
    assert names[3] == "All other users (7+)"                          # 7 named keys + the SQL bucket
    other = exp.by_user[3]
    assert other.delta_usd == round(sum(i * 5.0 for i in range(7)) + 7.0, 2)
    assert "(all other users)" not in names
    assert abs(sum(r.delta_usd for r in exp.by_user) - exp.metered_delta_usd) < 0.01
    # without the SQL bucket the count is exact
    plain = explain_below_warehouse(_many_users(), _D, "WH_A", max_rows=3)
    assert plain.by_user[3].name == "All other users (7)"


def test_company_mask_stays_its_own_row_and_leads_as_a_group_never_as_a_user():
    extra = [_rows(_D - timedelta(days=o), "USER", "(users outside ALFA)", 1.0) for o in range(1, 15)]
    extra += [_rows(_D - timedelta(days=o), "DATABASE", "DB0", 1.0) for o in range(1, 15)]
    extra += [_rows(_D, "USER", "(users outside ALFA)", 500.0), _rows(_D, "DATABASE", "DB0", 500.0)]
    extra += [_rows(_D - timedelta(days=o), "METERED", "WH_A", 1.0) for o in range(1, 15)]
    extra += [_rows(_D, "METERED", "WH_A", 500.0)]
    exp = explain_below_warehouse(_many_users(extra), _D, "WH_A", max_rows=2)
    names = [r.name for r in exp.by_user]
    assert names[0] == "(users outside ALFA)"                          # the biggest mover, sorted in
    assert names[1:3] == ["U9", "U8"]                                  # does not eat a max_rows slot
    # F16: it drives the move, so the narrative leads with it — as a GROUP, never as a named user
    assert "By user: the largest change is in users outside ALFA — $499.00 over their average" in exp.narrative
    assert "; the largest named user is U9 $45.00 over its average" in exp.narrative
    assert "By user: (users outside ALFA)" not in exp.narrative and "By user: U9" not in exp.narrative
    assert is_bucket_row("(users outside ALFA)") and is_bucket_row("All other users (7)")
    assert is_bucket_row(UNALLOCATED_LABEL) and not is_bucket_row("U9")


def test_a_dominant_masked_row_leads_the_narrative():
    """F16, the reviewer's case: an unclassified service login adds $500 on WH_ALFA_ETL while the
    only named user moves $20. The table sorts the mask first; the narrative used to skip it and lead
    with ALICE (+4%) — now it says where 96% of the move is, then names the largest real user."""
    def _frame(named_on_d: float) -> pd.DataFrame:
        rows = []
        for o in range(14, -1, -1):
            d = _D - timedelta(days=o)
            named, unc = (named_on_d, 500.0) if o == 0 else (100.0, 0.0)
            rows += [_rows(d, "SPINE", "", 0), _rows(d, "USER", "ALICE", named),
                     _rows(d, "DATABASE", "DB_MAIN", named + unc), _rows(d, "METERED", "WH_ALFA_ETL", named + unc)]
            if unc:
                rows.append(_rows(d, "USER", UNCLASSIFIED_USERS_LABEL, unc))
        return pd.DataFrame(rows)

    exp = explain_below_warehouse(_frame(120.0), _D, "WH_ALFA_ETL")
    assert exp.ok and exp.metered_delta_usd == 520.0
    assert [r.name for r in exp.by_user][:2] == ["(unclassified users)", "ALICE"]
    assert ("By user: the largest change is in unclassified users — $500.00 over their average (+96% of the "
            "move); the largest named user is ALICE $20.00 over its average (+4% of the move).") in exp.narrative
    assert "By database: DB_MAIN $520.00 over its average (+100% of the move)." in exp.narrative
    # a named key that moved MORE than the mask still leads, exactly as before
    named_first = explain_below_warehouse(_frame(900.0), _D, "WH_ALFA_ETL")
    assert "By user: ALICE $800.00 over its average (+62% of the move)." in named_first.narrative
    assert "largest change is in" not in named_first.narrative


def test_an_all_other_fold_that_outweighs_every_named_key_leads_too():
    exp = explain_below_warehouse(_many_users(), _D, "WH_A", max_rows=1)
    assert [r.name for r in exp.by_user][:2] == ["U9", "All other users (9)"]
    assert ("By user: the largest change is in all other users (9) — $180.00 over their average "
            "(+80% of the move); the largest named user is U9 $45.00 over its average") in exp.narrative


def test_the_not_allocated_residual_never_leads_the_by_user_clause():
    # idle jumps $980 while C moves $235.71: the idle clause reports it; 'By user' still names C
    exp = explain_below_warehouse(_sporadic(idle_d=1000.0), _D, "WH_A")
    assert exp.unallocated_delta_usd == 980.0
    assert "By user: C $235.71 over its average" in exp.narrative
    assert "$980.00 more than usual was not tied to a query" in exp.narrative
    assert "largest change is in" not in exp.narrative


def test_both_user_masks_are_their_own_rows_and_the_sums_stay_exact():
    extra = []
    for label, base, day in ((UNCLASSIFIED_USERS_LABEL, 2.0, 30.0), ("(users outside Trexis)", 3.0, 1.0)):
        extra += [_rows(_D - timedelta(days=o), "USER", label, base) for o in range(1, 15)]
        extra += [_rows(_D - timedelta(days=o), "DATABASE", "DB0", base) for o in range(1, 15)]
        extra += [_rows(_D - timedelta(days=o), "METERED", "WH_A", base) for o in range(1, 15)]
        extra += [_rows(_D, "USER", label, day), _rows(_D, "DATABASE", "DB0", day), _rows(_D, "METERED", "WH_A", day)]
    exp = explain_below_warehouse(_many_users(extra), _D, "WH_A", max_rows=1)
    names = [r.name for r in exp.by_user]
    assert names[:3] == ["U9", UNCLASSIFIED_USERS_LABEL, "(users outside Trexis)"]   # sorted in, no slot eaten
    assert names[3] == "All other users (9)"
    assert abs(sum(r.delta_usd for r in exp.by_user) - exp.metered_delta_usd) < 0.01


def test_bucket_detection_covers_every_label_the_reader_can_emit():
    """One source of truth (F16): the reader writes these exact strings, is_bucket_row detects them
    by exact string — every company's mask for both dimensions, the unclassified row, the fold."""
    labels = [UNCLASSIFIED_USERS_LABEL, *SQL_OTHER_LABELS.values(), UNALLOCATED_LABEL]
    labels += [outside_company_label(dim, c) for dim in ("USER", "DATABASE") for c in COMPANIES if c != "ALL"]
    assert UNCLASSIFIED_USERS_LABEL == "(unclassified users)"
    assert outside_company_label("USER", "ALFA") == "(users outside ALFA)"
    assert outside_company_label("DATABASE", "Trexis") == "(databases outside Trexis)"
    for label in labels:
        assert is_bucket_row(label), label
    # exact, not a pattern: a stray look-alike (or the retired label) is a real key
    for real in ("(users outside NOPE)", "(other-company users)", "users outside ALFA", "ALICE", "NONE"):
        assert not is_bucket_row(real), real


def test_share_is_suppressed_on_an_offsetting_day():
    rows = []
    for o in range(14, -1, -1):
        d = _D - timedelta(days=o)
        x, y = (200.0, 5.0) if o == 0 else (100.0, 100.0)             # X +100, Y -95: net +5 on 195 gross
        rows += [_rows(d, "SPINE", "", 0), _rows(d, "USER", "X", x), _rows(d, "USER", "Y", y),
                 _rows(d, "DATABASE", "DB", x + y), _rows(d, "METERED", "WH_A", x + y)]
    exp = explain_below_warehouse(pd.DataFrame(rows), _D, "WH_A")
    assert exp.ok and exp.metered_delta_usd == 5.0
    assert all(r.share_pct == 0.0 for r in exp.by_user)
    assert "% of the move" not in exp.narrative.split("By database")[0]


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------

def test_narrative_names_real_keys_says_average_and_gates_the_idle_clause():
    exp = explain_below_warehouse(_sporadic(), _D, "WH_A")
    n = exp.narrative
    assert n.startswith("WH_A on 2026-09-20: $460.00 metered vs a 14-day average of $184.29")
    assert "$275.71 above usual" in n
    assert "By user: C $235.71 over its average (+86% of the move)" in n
    assert "By database: DB_ADHOC $235.71 over" in n
    assert "average" in n and "median" not in n
    assert "$40.00 more than usual was not tied to a query" in n      # |40| >= max(1, 27.57)
    quiet = explain_below_warehouse(_sporadic(idle_d=20.0), _D, "WH_A")
    assert quiet.unallocated_delta_usd == 0.0 and "not tied to a query" not in quiet.narrative
    small = explain_below_warehouse(_sporadic(idle_d=40.0), _D, "WH_A")   # 20 < max(1, 10% of 255.71)
    assert small.unallocated_delta_usd == 20.0 and "not tied to a query" not in small.narrative


def test_an_in_line_day_says_so():
    rows = []
    for o in range(14, -1, -1):
        d = _D - timedelta(days=o)
        rows += [_rows(d, "SPINE", "", 0), _rows(d, "USER", "U", 10.0), _rows(d, "DATABASE", "DB", 10.0),
                 _rows(d, "METERED", "WH_A", 12.0)]
    exp = explain_below_warehouse(pd.DataFrame(rows), _D, "WH_A")
    assert exp.ok and exp.metered_delta_usd == 0.0 and "in line with its 14-day average" in exp.narrative


# ---------------------------------------------------------------------------
# changes_near_day
# ---------------------------------------------------------------------------

def _registry(rows: list[tuple[str, object]]) -> pd.DataFrame:
    return pd.DataFrame([
        {"WAREHOUSE_NAME": wh, "COMPANY": "ALFA", "SETTING": "SIZE", "OLD_VALUE": "MEDIUM",
         "NEW_VALUE": "LARGE", "CHANGE_SEEN_AT": seen, "CHANGED_BY": "DBA_JANE",
         "CHANGE_SOURCE": "MANUAL", "VERDICT": "REGRESSED", "VERDICT_DETAIL": ""}
        for wh, seen in rows])


def test_changes_near_day_matches_the_exact_warehouse_only():
    reg = _registry([("WH_A", datetime(2026, 9, 20, 6, 40)), ("WH_A_BIG", datetime(2026, 9, 20, 6, 40)),
                     ("wh_a", datetime(2026, 9, 21, 6, 40))])
    got = changes_near_day(reg, "WH_A", _D)
    assert [c["entity"] for c in got] == ["WH_A", "wh_a"]             # substring ILIKE matched WH_A_BIG too


def test_changes_near_day_window_is_account_local_plus_minus_one_day():
    chicago = "America/Chicago"
    reg = _registry([
        ("WH_A", pd.Timestamp("2026-09-21 06:40", tz=chicago)),        # D+1, account-local: kept
        ("WH_A", pd.Timestamp("2026-09-22 03:00", tz="UTC")),          # = Sep 21 22:00 CT: kept
        ("WH_A", pd.Timestamp("2026-09-19 04:00", tz="UTC")),          # = Sep 18 23:00 CT (D-2): dropped
        ("WH_A", datetime(2026, 9, 19, 6, 40)),                        # naive = account-local D-1: kept
        ("WH_A", datetime(2026, 9, 22, 6, 40)),                        # D+2: dropped
        ("WH_A", None),                                                # unknown time: dropped
    ])
    got = changes_near_day(reg, "WH_A", _D)
    whens = sorted(pd.Timestamp(c["when"]).tz_localize(None) if pd.Timestamp(c["when"]).tzinfo is None
                   else pd.Timestamp(c["when"]).tz_convert(chicago).tz_localize(None) for c in got)
    assert whens == [pd.Timestamp("2026-09-19 06:40"), pd.Timestamp("2026-09-21 06:40"),
                     pd.Timestamp("2026-09-21 22:00")]


def test_changes_near_day_titles_read_the_setting_not_the_source():
    got = changes_near_day(_registry([("WH_A", datetime(2026, 9, 20, 6, 40))]), "wh_a", "2026-09-20")
    assert len(got) == 1
    assert "SIZE: MEDIUM → LARGE" in got[0]["title"] and ": MANUAL" not in got[0]["title"]
    assert got[0]["changed_by"] == "DBA_JANE"


def test_changes_near_day_tolerates_missing_inputs():
    assert changes_near_day(None, "WH_A", _D) == []
    assert changes_near_day(pd.DataFrame(), "WH_A", _D) == []
    assert changes_near_day(_registry([("WH_A", datetime(2026, 9, 20))]), "", _D) == []
    assert changes_near_day(_registry([("WH_A", datetime(2026, 9, 20))]), "WH_A", None) == []
    assert changes_near_day(pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"]}), "WH_A", _D) == []
