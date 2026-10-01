"""Next-Fifty #33: per-warehouse statement-timeout posture (Operations ▸ Warehouses ▸ Sizing & efficiency),
plus the two timeout follow-ups that ship with it: F1 the alert drawer's "Statement timeout 1h" lever is
tighten-only, F2 is locked in history_locks/test_wave2_riders.py. v4.603 follow-ups (probe answers
2026-09-29, W5/W5b): D1 the 1h lever reads one warehouse's 30-day impact and withholds the ALTER behind an
explicit override when it would cancel anything (or the impact is unknown), D3 the Emergency lever's 0 is the
7-day maximum, D4 Snowflake-managed COMPUTE_SERVICE_WH* pools get their own status, D5 'Timed out' is paired
with the ceiling that fired. D2 (Admin's V002 wording) is locked in test_admin_timeout_wording.py.

Pure, builder, fake-rendered and source locks, on both CI legs (the shaped AppTests are in
test_prc_c1_shaped.py)."""

from __future__ import annotations

import math
import re

import pandas as pd
import pytest

from app import config
from app.data import canary, ops_sql
from app.logic import remediation, stmt_timeout
from app.logic.stmt_timeout import (
    CAP_LADDER_S,
    DISPLAY_COLUMNS,
    POSTURE_COLUMNS,
    STATUS_CAPPED,
    STATUS_MANAGED,
    STATUS_NOT_VISIBLE,
    STATUS_UNCAPPED,
    STATUS_UNREAD,
    TimeoutImpact,
    derive_account_timeout,
    effective_timeout_s,
    fired_at_text,
    fired_below_cap,
    fix_script,
    is_managed_compute,
    is_uncapped,
    parse_timeout_impact,
    parse_timeout_row,
    posture_summary,
    status_notes,
    suggested_cap_s,
    tail_window_days,
    tighten_timeout_plan,
    timeout_posture,
    timeout_would_tighten,
    warehouse_universe,
)
from tests._source import page_source, read


def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


# ---------------------------------------------------------------------------
# semantics
# ---------------------------------------------------------------------------

def test_effective_cap_is_lowest_nonzero():
    assert effective_timeout_s(172800, "", None) == (172800, "Snowflake default")
    assert effective_timeout_s(0, "WAREHOUSE", None) == (604800, "Warehouse")        # 0 = the 7-day max
    assert effective_timeout_s(7200, "WAREHOUSE", 3600) == (3600, "Account")          # the lower one wins
    assert effective_timeout_s(0, "WAREHOUSE", 3600) == (3600, "Account")             # 0 is not "lowest"
    assert effective_timeout_s(300, "WAREHOUSE", 172800) == (300, "Warehouse")
    assert effective_timeout_s(3600, "ACCOUNT", 3600) == (3600, "Account")            # inherited
    assert effective_timeout_s(900000, "WAREHOUSE", None) == (604800, "Warehouse")    # clamped
    assert effective_timeout_s(None, "", 3600) == (None, "")                          # unread stays unread
    assert is_uncapped(172800) and is_uncapped(604800)
    assert not is_uncapped(3600) and not is_uncapped(172799) and not is_uncapped(None)


def test_parse_show_parameters_row():
    up = pd.DataFrame([{"KEY": "STATEMENT_TIMEOUT_IN_SECONDS", "VALUE": "300", "LEVEL": "WAREHOUSE"}])
    low = pd.DataFrame([{"key": "STATEMENT_TIMEOUT_IN_SECONDS", "value": "172800", "level": ""}])
    assert parse_timeout_row(up) == (300.0, "WAREHOUSE")
    assert parse_timeout_row(low) == (172800.0, "")
    assert parse_timeout_row(pd.DataFrame([{"value": "7200", "level": None}])) == (7200.0, "")
    assert parse_timeout_row(pd.DataFrame([{"value": "7200"}])) == (7200.0, "")       # no level column
    assert parse_timeout_row(pd.DataFrame()) == (None, "")
    assert parse_timeout_row(None) == (None, "")
    assert parse_timeout_row(pd.DataFrame([{"key": "X", "level": "ACCOUNT"}])) == (None, "")
    assert parse_timeout_row(pd.DataFrame([{"value": "abc", "level": "ACCOUNT"}])) == (None, "")


def test_derive_account_timeout():
    assert derive_account_timeout([(300.0, "WAREHOUSE"), (3600.0, "ACCOUNT")]) == 3600.0
    assert derive_account_timeout([(300.0, "WAREHOUSE"), (172800.0, "")]) == 172800.0
    assert derive_account_timeout([(300.0, "WAREHOUSE"), (None, "")]) is None     # an unread row proves nothing
    assert derive_account_timeout([(300.0, "WAREHOUSE")]) is None
    assert derive_account_timeout([]) is None


def test_suggested_cap_ladder():
    assert suggested_cap_s(4.2, 5000) == 300                  # the floor
    assert suggested_cap_s(250, 5000) == 900
    assert suggested_cap_s(700, 5000) == 3600
    assert suggested_cap_s(300, 5000) == 900                  # exactly 3x lands ON a rung
    assert suggested_cap_s(40000, 5000) is None              # x3 above the 24h top step
    assert suggested_cap_s(250, 99) is None                  # too few runs for a p99
    assert suggested_cap_s(float("nan"), 5000) is None
    assert suggested_cap_s(None, 5000) is None and suggested_cap_s(250, None) is None
    for p99 in (0.5, 99.0, 101.0, 599.0, 4800.0, 28799.0):
        cap = suggested_cap_s(p99, 1000)
        assert cap in CAP_LADDER_S and cap >= p99 * 3


def test_tail_window_days():
    assert [tail_window_days(d) for d in (1, 7, 30, 45, 90, 365, "x", None)] == [30, 30, 30, 45, 90, 90, 30, 30]


# ---------------------------------------------------------------------------
# universe + posture frame + script
# ---------------------------------------------------------------------------

def _tail(*rows) -> pd.DataFrame:
    """(name, runs, p99, max, timed_out, {rung: over}[, (fired_min, fired_max)]) -> a tail frame."""
    out = []
    for name, runs, p99, mx, timed_out, over, *fired in rows:
        lo, hi = fired[0] if fired else (None, None)
        r = {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "COMPLETED_RUNS": runs, "P99_ELAPSED_SEC": p99,
             "MAX_ELAPSED_SEC": mx, "TIMEOUT_CANCELLED_RUNS": timed_out, "TIMEOUT_FIRED_MIN_SEC": lo,
             "TIMEOUT_FIRED_MAX_SEC": hi, "TIMEOUT_CANCELLED_TOTAL": 7}
        r.update({f"RUNS_OVER_{s}": over.get(s, 0) for s in CAP_LADDER_S})
        out.append(r)
    return pd.DataFrame(out)


def test_warehouse_universe():
    # v4.603 (#33 D4) moved this lock: the universe is now a 3-tuple (read, not visible, managed compute)
    show = pd.DataFrame({"name": ["WH_A", "WH_B", "WH_IDLE"]})
    tail = _tail(("WH_A", 5000, 250.0, 4000.0, 0, {}), ("WH_GONE", 10, 1.0, 2.0, 0, {}))
    assert warehouse_universe(show, tail, "ALL") == (["WH_A", "WH_B", "WH_IDLE"], ["WH_GONE"], [])
    # a company scope reads only the (UDF-scoped) tail warehouses SHOW also lists
    assert warehouse_universe(show, tail, "ALFA") == (["WH_A"], ["WH_GONE"], [])
    # SHOW unusable: fall back to the tail names, and claim nothing about visibility
    assert warehouse_universe(None, tail, "ALL") == (["WH_A", "WH_GONE"], [], [])
    assert warehouse_universe(pd.DataFrame(), None, "ALL") == ([], [], [])


# W5b (answers 2026-09-29): the 30-day tail named 28 warehouses, SHOW (as SNOW_ACCOUNTADMINS) listed 25;
# the 7 names only the tail saw are Snowflake-managed compute pools.
_W5B_MANAGED = ["COMPUTE_SERVICE_WH", "COMPUTE_SERVICE_WH_NATIVE_APPLICATION_UPGRADE_POOL_STANDARD",
                "COMPUTE_SERVICE_WH_SNOWFLAKEDB_UPGRADE_POOL_STANDARD",
                "COMPUTE_SERVICE_WH_USER_TASKS_POOL_STANDARD_GEN1_XSMALL"]


def test_managed_compute_pools_leave_not_visible():
    """#33 D4: a COMPUTE_SERVICE_WH* name SHOW does not list is Snowflake-managed compute (serverless-task /
    upgrade pools: no role can SHOW them, no warehouse timeout to set), not a dropped / renamed / hidden
    warehouse. Only names SHOW does NOT list are classified that way."""
    show = pd.DataFrame({"name": ["WH_A", "COMPUTE_SERVICE_WH_MINE"]})     # a user warehouse with the prefix
    tail = _tail(("WH_A", 5000, 250.0, 4000.0, 0, {}), ("WH_GONE", 10, 1.0, 2.0, 0, {}),
                 ("COMPUTE_SERVICE_WH_MINE", 500, 3.0, 9.0, 0, {}),
                 *[(n, 3718, 7.0, 704.0, 0, {}) for n in _W5B_MANAGED],
                 ("compute_service_wh_lower", 5, 1.0, 1.0, 0, {}))
    read, hidden, managed = warehouse_universe(show, tail, "ALL")
    assert read == ["COMPUTE_SERVICE_WH_MINE", "WH_A"]                   # SHOW lists it: read like any other
    assert hidden == ["WH_GONE"]
    assert managed == sorted([*_W5B_MANAGED, "compute_service_wh_lower"])
    assert is_managed_compute(" compute_service_wh_x ") and not is_managed_compute("WH_COMPUTE_SERVICE_WH")
    # SHOW unusable: nothing is classified (the tail names are read, as before)
    assert warehouse_universe(None, tail, "ALL")[1:] == ([], [])
    # the posture rows: their own status, sorted after Not visible, tail values kept, no cap / suggestion
    params = {"WH_A": (3600.0, "WAREHOUSE"), "COMPUTE_SERVICE_WH_MINE": (3600.0, "WAREHOUSE")}
    p = timeout_posture(read, params, 21600.0, tail, hidden, managed)
    assert list(p["STATUS"]) == [STATUS_CAPPED] * 2 + [STATUS_NOT_VISIBLE] + [STATUS_MANAGED] * 5
    row = p.set_index("WAREHOUSE_NAME").loc["COMPUTE_SERVICE_WH_USER_TASKS_POOL_STANDARD_GEN1_XSMALL"]
    assert row["COMPLETED_RUNS"] == 3718 and row["MAX_ELAPSED_SEC"] == 704.0
    assert math.isnan(row["EFFECTIVE_TIMEOUT_SEC"]) and row["CAP_SOURCE"] is None
    summ = posture_summary(p)
    assert (summ["not_visible"], summ["managed"], summ["read"]) == (1, 5, 2)
    assert fix_script(p, 30) == ""                                     # never scripted


def test_status_notes_name_each_cause():
    notes = status_notes({"not_visible": 1, "managed": 7, "fired_below": []})
    assert len(notes) == 2
    assert notes[0].startswith("1 warehouse(s) ran statements in the window but SHOW WAREHOUSES does not list")
    assert "dropped, renamed, or not visible to the app role" in notes[0]
    assert notes[1].startswith("7 Snowflake-managed compute pool(s) (COMPUTE_SERVICE_WH*: serverless-task")
    assert "no warehouse timeout to set" in notes[1] and "USER_TASK_TIMEOUT_MS" in notes[1]
    assert "dropped" not in notes[1] and "not visible to the app role" not in notes[1]
    assert status_notes({"not_visible": 0, "managed": 0, "fired_below": []}) == []
    below = status_notes({"fired_below": ["WH_ALFA_QUERY", "WH_ALFA_TRANSFORM_PRD"]})
    # review R1-19: 'below cap' judges the LOWEST ceiling that fired (a straddling range is flagged too), so
    # the note says the lowest one, not "the ceiling that fired"
    # review R2-10: the causes name 'task' (as the adjacent 'Timed out' texts do) and an earlier account value,
    # and never attribute the ceiling to one level: the parse reads the number only
    assert below == ["Timed out below the effective cap on WH_ALFA_QUERY, WH_ALFA_TRANSFORM_PRD: the lowest "
                     "ceiling that fired ('Fired at') is lower than the warehouse's cap, so at least some of "
                     "those cancels were not that cap. The lower ceiling is a user, session, client or task "
                     "value, or an earlier, lower warehouse or account value (the message gives the number of "
                     "seconds, not which of these set it)."]
    many = status_notes({"fired_below": [f"WH_{i}" for i in range(9)]})[0]
    assert "WH_5 and 3 more:" in many and "WH_6" not in many


def test_warehouse_universe_caps_the_reads_longest_first():
    names = [f"WH_{i:03d}" for i in range(stmt_timeout.MAX_WAREHOUSES_READ + 20)]
    show = pd.DataFrame({"name": names})
    last = names[-1]      # (was the literal "WH_119" while the cap was 100; review C21 lowered it to 40)
    tail = _tail((last, 500, 10.0, 99999.0, 0, {}))           # the last by name, but the longest run
    read, hidden, managed = warehouse_universe(show, tail, "ALL")
    assert len(read) == stmt_timeout.MAX_WAREHOUSES_READ and read == sorted(read)
    assert last in read and hidden == [] and managed == []


def test_read_cap_stays_within_a_third_of_the_metadata_cache():
    """Review C21: every warehouse SHOW is its own entry in the process-wide metadata-tier st.cache_data
    store; one toggle must not evict the other metadata reads (SHOW DATABASES, the user directory, the
    schema gates, the Admin probes). The per-warehouse reads + the account SHOW + SHOW WAREHOUSES stay
    within a third of that store."""
    src = read("app/core/query.py")
    m = re.search(r'@st\.cache_data\(ttl=CACHE_TTLS\["metadata"\], show_spinner=False, max_entries=(\d+)\)\n'
                  r"def _fetch_metadata\(", src)
    assert m, "the metadata fetcher's cache size moved: re-check the timeout panel's read cap"
    assert int(m.group(1)) // 3 >= stmt_timeout.MAX_WAREHOUSES_READ + 2
    body = _body(page_source("operations"), "_stmt_timeout_posture_panel")
    assert "if len(names) >= stmt_timeout.MAX_WAREHOUSES_READ:" in body        # the cap stays disclosed
    assert "Reads at most {stmt_timeout.MAX_WAREHOUSES_READ} warehouses per view" in body


def test_account_value_kpi_shows_the_enforced_value():
    """Review C15/C20: an account set to 0 enforces the 7-day maximum; the tile must not read '0s'."""
    zero = stmt_timeout.account_value_kpi(0.0, "read")
    assert zero["label"] == "Account value" and zero["value"] == "168h"
    assert zero["delta"] == "read; 0 = 7-day max" and "set to 0" in zero["help"] and zero["delta_color"] == "off"
    derived = stmt_timeout.account_value_kpi(0.0, "derived from warehouse rows")
    assert derived["value"] == "168h" and derived["delta"] == "derived from warehouse rows; 0 = 7-day max"
    assert stmt_timeout.account_value_kpi(172800.0, "read")["value"] == "48h"
    plain = stmt_timeout.account_value_kpi(3600.0, "read")
    assert plain["value"] == "1h" and plain["delta"] == "read" and "set to 0" not in plain["help"]
    unread = stmt_timeout.account_value_kpi(None, "unread")
    assert unread["value"] == "—" and unread["delta"] == "unread"
    body = _body(page_source("operations"), "_stmt_timeout_posture_panel")
    assert "stmt_timeout.account_value_kpi(account_s, acct_how)" in body
    assert "humanize_duration(account_s)" not in body                       # the raw value never headlines


def test_empty_universe_state_never_calls_a_failed_read_empty():
    """Review C16 (house rule 8): a company scope lists only the runtime tail's warehouses, so a failed tail
    is 'unavailable', never the quiet 'no data yet'."""
    kind, msg, which = stmt_timeout.empty_universe_state("ALFA", tail_ok=False, show_ok=True)
    assert (kind, which) == ("unavailable", "tail") and "could not be read" in msg
    assert stmt_timeout.empty_universe_state("ALFA", tail_ok=True, show_ok=True)[:1] == ("no_data_yet",)
    gone = stmt_timeout.empty_universe_state("Trexis", tail_ok=True, show_ok=True, active=3)
    assert gone[0] == "no_data_yet" and "3 warehouse(s)" in gone[1]
    # ALL scope lists SHOW WAREHOUSES (the tail is only its fallback)
    show_down = stmt_timeout.empty_universe_state("ALL", tail_ok=True, show_ok=False)
    assert show_down[0] == "unavailable" and show_down[2] == "show"
    both_down = stmt_timeout.empty_universe_state("ALL", tail_ok=False, show_ok=False)
    assert both_down[0] == "unavailable" and "neither could the runtime tail" in both_down[1]
    assert stmt_timeout.empty_universe_state("ALL", tail_ok=False, show_ok=True)[0] == "no_data_yet"
    # #33 D4: a company scope whose active names are all Snowflake-managed compute names that cause
    only_managed = stmt_timeout.empty_universe_state("UNKNOWN", tail_ok=True, show_ok=True, managed=7)
    assert only_managed[0] == "no_data_yet" and "Snowflake-managed compute" in only_managed[1]
    assert "7 warehouse(s)" in only_managed[1] and "dropped" not in only_managed[1]
    both = stmt_timeout.empty_universe_state("UNKNOWN", tail_ok=True, show_ok=True, active=2, managed=7)
    assert "9 warehouse(s)" in both[1] and "(2: dropped" in both[1] and "(7: COMPUTE_SERVICE_WH*" in both[1]
    body = _body(page_source("operations"), "_stmt_timeout_posture_panel")
    # v4.603 moved this lock: the call also passes the managed-compute count
    assert ("stmt_timeout.empty_universe_state(company, tail_ok=tail.ok, show_ok=whs.ok,\n"
            "                                                                active=len(not_visible), "
            "managed=len(managed))") in body
    assert 'empty_state("unavailable" if not (tail.ok or whs.ok) else "no_data_yet"' not in body


def _posture() -> pd.DataFrame:
    tail = _tail(("WH_A", 5000, 250.0, 4000.0, 2, {900: 12, 300: 90}),
                 ("WH_B", 800, 40.0, 7000.0, 1, {}),
                 ("WH_LOW", 20, 5.0, 60.0, 0, {}),
                 ("WH_GONE", 10, 1.0, 2.0, 0, {}))
    params = {"WH_A": (172800.0, ""), "WH_B": (300.0, "WAREHOUSE"), "WH_LOW": (0.0, "WAREHOUSE"),
              "WH_ERR": (None, ""), "WH_IDLE": (172800.0, "")}
    return timeout_posture(["WH_A", "WH_B", "WH_ERR", "WH_IDLE", "WH_LOW"], params, 172800.0, tail,
                           not_visible=["WH_GONE"])


def test_timeout_posture_frame():
    p = _posture()
    assert list(p.columns) == POSTURE_COLUMNS
    assert POSTURE_COLUMNS[:11] == DISPLAY_COLUMNS and DISPLAY_COLUMNS[8] == "TIMEOUT_FIRED"
    assert list(p["STATUS"]) == [STATUS_UNCAPPED] * 3 + [STATUS_CAPPED, STATUS_UNREAD, STATUS_NOT_VISIBLE]
    by = p.set_index("WAREHOUSE_NAME")
    assert list(p["WAREHOUSE_NAME"][:3]) == ["WH_A", "WH_LOW", "WH_IDLE"]   # longest run first, NaN last
    assert by.loc["WH_A", "SUGGESTED_TIMEOUT_SEC"] == 900
    assert by.loc["WH_A", "WOULD_CANCEL_RUNS"] == 12                        # == RUNS_OVER_<suggested>
    assert by.loc["WH_A", "CAP_SOURCE"] == "Snowflake default"
    assert by.loc["WH_LOW", "EFFECTIVE_TIMEOUT_SEC"] == 172800             # 0 -> 7d, the account 2d wins
    assert by.loc["WH_LOW", "CAP_SOURCE"] == "Account"
    assert math.isnan(by.loc["WH_LOW", "SUGGESTED_TIMEOUT_SEC"])            # 20 runs: no p99 suggestion
    # a capped warehouse never gets a suggestion; an idle / unread one keeps NaN tails (never 0)
    assert math.isnan(by.loc["WH_B", "SUGGESTED_TIMEOUT_SEC"]) and by.loc["WH_B", "EFFECTIVE_TIMEOUT_SEC"] == 300
    for col in ("COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC", "TIMEOUT_CANCELLED_RUNS"):
        assert math.isnan(by.loc["WH_IDLE", col]) and math.isnan(by.loc["WH_ERR", col])
    assert math.isnan(by.loc["WH_ERR", "EFFECTIVE_TIMEOUT_SEC"]) and by.loc["WH_ERR", "CAP_SOURCE"] is None
    assert by.loc["WH_GONE", "COMPLETED_RUNS"] == 10                       # not visible, but its tail shows
    assert posture_summary(p) == {"read": 4, "uncapped": 3, "capped": 1, "unread": 1, "not_visible": 1,
                                  "managed": 0, "fired_below": []}
    assert posture_summary(pd.DataFrame())["read"] == 0


def test_fired_at_is_tied_to_the_ceiling_that_fired():
    """#33 D5: a cancel fires at whichever ceiling is lowest for THAT statement. W5b (2026-09-29, account
    21600 s): none of the ~22 cancels fired at the 6h account cap -- WH_ALFA_QUERY at 2-10 s, WH_ALFA_TRANSFORM_PRD
    at 600 s (both inherit 6h: a user / session / client value fired below it), WH_ALFA_ADMIN at 1800 s and
    WH_TRXS_TRANSFORM at 7200 s (each at its own warehouse value here: the count matches the cap)."""
    tail = _tail(("WH_ALFA_QUERY", 974168, 4.0, 6008.0, 2, {3600: 6}, (2.0, 10.0)),
                 ("WH_ALFA_TRANSFORM_PRD", 647023, 160.0, 4381.0, 9, {3600: 12}, (600.0, 600.0)),
                 ("WH_ALFA_ADMIN", 109188, 91.0, 1801.0, 9, {}, (1800.0, 1800.0)),
                 ("WH_TRXS_TRANSFORM", 5306096, 40.0, 9245.0, 1, {3600: 27}, (7200.0, 7200.0)),
                 ("WH_TRXS_QUERY", 103396, 17.0, 3551.0, 0, {}),
                 ("WH_ALFA_OLD", 50, 1.0, 5.0, 3, {}, (1800.0, 1800.0)))
    params = {"WH_ALFA_QUERY": (21600.0, "ACCOUNT"), "WH_ALFA_TRANSFORM_PRD": (21600.0, "ACCOUNT"),
              "WH_ALFA_ADMIN": (1800.0, "WAREHOUSE"), "WH_TRXS_TRANSFORM": (7200.0, "WAREHOUSE"),
              "WH_TRXS_QUERY": (21600.0, "ACCOUNT")}
    p = timeout_posture(list(params), params, 21600.0, tail, not_visible=["WH_ALFA_OLD"]).set_index("WAREHOUSE_NAME")
    assert p.loc["WH_ALFA_QUERY", "TIMEOUT_FIRED"] == "2.0s–10s · below cap"
    assert p.loc["WH_ALFA_QUERY", "FIRED_BELOW_CAP"] is True
    assert (p.loc["WH_ALFA_QUERY", "TIMEOUT_FIRED_MIN_SEC"], p.loc["WH_ALFA_QUERY", "TIMEOUT_FIRED_MAX_SEC"]) == (2, 10)
    assert p.loc["WH_ALFA_TRANSFORM_PRD", "TIMEOUT_FIRED"] == "10m · below cap"
    assert p.loc["WH_ALFA_ADMIN", "TIMEOUT_FIRED"] == "30m" and p.loc["WH_ALFA_ADMIN", "FIRED_BELOW_CAP"] is False
    assert p.loc["WH_TRXS_TRANSFORM", "TIMEOUT_FIRED"] == "2h" and p.loc["WH_TRXS_TRANSFORM", "FIRED_BELOW_CAP"] is False
    # no cancel: a dash (None), never '0s'; a not-visible row keeps its fired value but makes no cap judgement
    assert p.loc["WH_TRXS_QUERY", "TIMEOUT_FIRED"] is None and p.loc["WH_TRXS_QUERY", "FIRED_BELOW_CAP"] is None
    assert p.loc["WH_ALFA_OLD", "TIMEOUT_FIRED"] == "30m" and p.loc["WH_ALFA_OLD", "FIRED_BELOW_CAP"] is None
    summ = posture_summary(p.reset_index())
    assert summ["fired_below"] == ["WH_ALFA_QUERY", "WH_ALFA_TRANSFORM_PRD"]
    # the pure pieces
    assert fired_below_cap(600, 21600) is True and fired_below_cap(1800, 1800) is False
    assert fired_below_cap(None, 21600) is None and fired_below_cap(600, float("nan")) is None
    assert fired_at_text(None, None) is None and fired_at_text(3600, None) == "1h"
    assert fired_at_text(7200, 7200, True) == "2h · below cap"


def test_fired_below_cap_judges_the_lowest_ceiling_that_fired():
    """Review R1-19: FIRED_BELOW_CAP judges the LOWEST ceiling that fired (TIMEOUT_FIRED_MIN_SEC), not the
    highest. A range that straddles the cap (cancels at 600 s AND at the 6h cap itself) IS flagged: some of its
    cancels were not that cap firing (stmt_timeout.FIRED_BELOW_CAUSES), and
    that is what 'below cap' tells the reader. (spec_s2 proposed MAX < EFFECTIVE, which would flag a warehouse
    only when EVERY cancel fired below its cap and hide the straddle; MIN is the rule the code documents.)"""
    tail = _tail(("WH_MIX", 5000, 100.0, 21000.0, 3, {}, (600.0, 21600.0)),
                 ("WH_AT", 5000, 100.0, 21000.0, 3, {}, (21600.0, 21600.0)),
                 ("WH_NONE", 5000, 100.0, 21000.0, 0, {}))
    params = {"WH_MIX": (21600.0, "ACCOUNT"), "WH_AT": (21600.0, "ACCOUNT"), "WH_NONE": (21600.0, "ACCOUNT")}
    p = timeout_posture(list(params), params, 21600.0, tail).set_index("WAREHOUSE_NAME")
    assert p.loc["WH_MIX", "TIMEOUT_FIRED"] == "10m–6h · below cap"
    assert p.loc["WH_MIX", "FIRED_BELOW_CAP"] is True
    assert p.loc["WH_AT", "TIMEOUT_FIRED"] == "6h" and p.loc["WH_AT", "FIRED_BELOW_CAP"] is False
    assert p.loc["WH_NONE", "FIRED_BELOW_CAP"] is None                    # no cancel: no judgement
    summ = posture_summary(p.reset_index())
    assert summ["fired_below"] == ["WH_MIX"]
    (note,) = status_notes(summ)
    assert note.startswith("Timed out below the effective cap on WH_MIX: the lowest ceiling that fired")


def test_fired_at_help_names_every_cause_of_below_cap():
    """Review R1-6/R1-12/R2-10: the 'Fired at' column help names the same causes as status_notes and
    fired_below_cap, verbatim (FIRED_BELOW_CAUSES): a user, session, client or TASK value -- the same levels
    the 'Timed out' help lists beside it -- OR an earlier, lower warehouse or account value (SHOW reads today's
    cap, the window reaches back 30-90 days), and says the message gives only the number, so the hover text
    never blames one level for a ceiling the parse cannot attribute."""
    import ast
    tree = ast.parse(read("app/ui/pages/operations.py"))
    helps = [kw.value.value for node in ast.walk(tree) if isinstance(node, ast.Call)
             and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "Fired at"
             for kw in node.keywords if kw.arg == "help" and isinstance(kw.value, ast.Constant)]
    assert len(helps) == 1, helps
    (help_,) = helps
    assert "'below cap' = the lowest of them is under this warehouse's effective cap" in help_
    from app.logic.stmt_timeout import FIRED_BELOW_CAUSES
    assert FIRED_BELOW_CAUSES in help_ and "task" in FIRED_BELOW_CAUSES
    assert "an earlier, lower warehouse or account value" in FIRED_BELOW_CAUSES
    assert "not which of these set it" in FIRED_BELOW_CAUSES
    assert "today's" in help_ and "30-90 days" in help_
    (note,) = status_notes({"fired_below": ["WH_A"]})
    assert f"The lower ceiling is {FIRED_BELOW_CAUSES}." in note
    # every level the adjacent 'Timed out' texts list, except today's effective cap itself, is a cause
    for level in ("user", "session", "client", "task", "warehouse", "account"):
        assert level in FIRED_BELOW_CAUSES, level
    runbook = re.sub(r"\s+", " ", read("RUNBOOK.md"))
    assert '"below cap" means the lowest ceiling that fired is under the warehouse\'s effective cap' in runbook
    below = runbook.split('"below cap" means', 1)[1][:400]
    assert "a user, session, client or task value" in below
    assert "an earlier, lower warehouse or account value" in below
    glossary = read("FEATURE_GLOSSARY.md")
    assert "a user, session, client or task value, or an earlier, lower warehouse or account value" in glossary
    assert "a user, session or client value" not in glossary
    # review R1-7/R1-10: the probe's 27 counted every status; the drawer counts completed statements only
    assert "ran 27 such statements" not in runbook
    assert "27 statements of 1h or more in 30 days, any status" in runbook and "at most 26" in runbook


def test_fix_script_review_only():
    p = _posture()
    script = fix_script(p, 45)
    lines = script.splitlines()
    assert lines[0].startswith("-- Review only: OVERWATCH never runs these.") and "last 45 days" in lines[0]
    alters = [ln for ln in lines if not ln.startswith("--")]
    assert alters == [remediation.statement_timeout_fix("WH_A", 900)]      # ONE live statement
    i = lines.index(alters[0])
    assert lines[i - 1].startswith("-- WH_A: effective cap 48h (Snowflake default); p99 4m 10s")
    assert "would have cancelled 12 of them" in lines[i - 1]
    assert lines[i + 1] == "-- undo: ALTER WAREHOUSE WH_A UNSET STATEMENT_TIMEOUT_IN_SECONDS;"
    assert any(ln.startswith("-- WH_LOW: uncapped; only 20 completed statements") and
               ln.endswith("choose a cap by hand.") for ln in lines)
    assert any(ln.startswith("-- WH_IDLE: uncapped; no completed statements") for ln in lines)
    assert "WH_B" not in script                                           # capped: never an ALTER
    # nothing uncapped -> no script; a capped-only frame never writes one
    assert fix_script(p[p["STATUS"] != STATUS_UNCAPPED], 30) == ""
    assert fix_script(pd.DataFrame(columns=POSTURE_COLUMNS), 30) == ""


def test_a_failed_tail_read_is_never_reported_as_no_statements():
    # the runtime read failed: every tail value is NaN, and the hand line must say so
    params = {"WH_A": (172800.0, "")}
    p = timeout_posture(["WH_A"], params, 172800.0, None)
    assert math.isnan(p.loc[0, "COMPLETED_RUNS"]) and math.isnan(p.loc[0, "SUGGESTED_TIMEOUT_SEC"])
    failed = fix_script(p, 30, tail_ok=False)
    assert "the runtime tail could not be read" in failed and "no completed statements" not in failed
    assert "no completed statements in the last 30 days" in fix_script(p, 30)   # a read that returned none


def test_fix_script_undo_restores_a_warehouse_level_value_and_hand_writes_quoted_names():
    tail = _tail(("WH_X", 5000, 250.0, 4000.0, 0, {900: 3}), ("wh lower", 5000, 250.0, 4000.0, 0, {900: 3}),
                 ("EVIL\nDROP TABLE T;", 5000, 250.0, 4000.0, 0, {}))
    params = {"WH_X": (0.0, "WAREHOUSE"), "wh lower": (172800.0, ""), "EVIL\nDROP TABLE T;": (172800.0, "")}
    p = timeout_posture(list(params), params, 172800.0, tail)
    script = fix_script(p, 30)
    assert "ALTER WAREHOUSE WH_X SET STATEMENT_TIMEOUT_IN_SECONDS = 900;" in script
    assert "-- undo: ALTER WAREHOUSE WH_X SET STATEMENT_TIMEOUT_IN_SECONDS = 0;" in script
    assert "-- wh lower: uncapped; its name needs a quoted identifier" in script
    assert "WH LOWER" not in script                                    # never ALTERed under the wrong name
    # a name carrying a newline cannot break out of its comment line into a statement
    assert [ln for ln in script.splitlines() if not ln.startswith("--")] == [
        "ALTER WAREHOUSE WH_X SET STATEMENT_TIMEOUT_IN_SECONDS = 900;"]


# ---------------------------------------------------------------------------
# F1: the alert drawer's "Statement timeout 1h" lever is tighten-only
# ---------------------------------------------------------------------------

def test_tighten_timeout_plan_never_loosens():
    tight = tighten_timeout_plan("WH_ADMIN", 300.0, "WAREHOUSE")
    assert tight["stmt"] == "" and tight["level"] == "info" and "already capped at 5m" in tight["message"]
    assert not tight["override_needed"]
    assert tighten_timeout_plan("WH_A", 3600.0, "WAREHOUSE")["stmt"] == ""        # equal: keep, no-op
    unread = tighten_timeout_plan("WH_A", None)
    assert unread["stmt"] == "" and unread["level"] == "warning" and not unread["override_needed"]
    # v4.603 (#33 D1) moved this lock: a tightening with NO impact read (impact=None) used to return the ALTER
    # at level 'none' with no message; it is now an UNKNOWN impact -- a warning, and the ALTER is withheld
    # until the override is ticked (never silent, never "no impact")
    default = tighten_timeout_plan("WH_A", 172800.0, "")
    assert default["stmt"] == "" and default["undo"] == "" and default["level"] == "warning"
    assert default["override_needed"] and default["message"].startswith("Impact unknown:")
    assert "was not read" in default["message"] and "not known (not zero)" in default["message"]
    forced = tighten_timeout_plan("WH_A", 172800.0, "", override=True)
    assert forced["stmt"] == "ALTER WAREHOUSE WH_A SET STATEMENT_TIMEOUT_IN_SECONDS = 3600;"
    assert forced["undo"] == "ALTER WAREHOUSE WH_A UNSET STATEMENT_TIMEOUT_IN_SECONDS;"
    none = TimeoutImpact(ok=True, over_target=0, exec_over_target=0, longest_s=1200.0)
    own = tighten_timeout_plan("WH_A", 7200.0, "WAREHOUSE", impact=none)
    assert own["undo"] == "ALTER WAREHOUSE WH_A SET STATEMENT_TIMEOUT_IN_SECONDS = 7200;"
    assert tighten_timeout_plan("WH_A", 0.0, "WAREHOUSE", impact=none)["stmt"].endswith("= 3600;")   # 0 = 7 days
    assert timeout_would_tighten(7200.0) and timeout_would_tighten(0.0)
    assert not timeout_would_tighten(3600.0) and not timeout_would_tighten(300.0) and not timeout_would_tighten(None)


# W5b (answers 2026-09-29): WH_TRXS_TRANSFORM ran 27 statements of 1h or more in 30 days (longest 9245 s) and
# fires at 7200 s; a 1h warehouse cap would override the 2h client session value those ETL statements use.
_TRXS_27 = TimeoutImpact(ok=True, over_target=27, exec_over_target=19, longest_s=9245.0, days=30)


def test_a_one_hour_cap_that_would_cancel_real_statements_is_withheld():
    """#33 D1: on this account the drawer's 1h lever named for WH_TRXS_TRANSFORM (value in force 2h) is a
    tightening, but it would have cancelled 27 completed statements -- warning, no ALTER until overridden."""
    plan = tighten_timeout_plan("WH_TRXS_TRANSFORM", 7200.0, "WAREHOUSE", impact=_TRXS_27)
    assert plan["level"] == "warning" and plan["stmt"] == "" and plan["undo"] == ""
    assert plan["override_needed"]
    msg = plan["message"]
    assert "would have cancelled 27 completed statements in the last 30 days (longest 2h 34m)" in msg
    assert "19 of them ran over 1h on execution time alone" in msg and "includes queue and compile time" in msg
    assert "The warehouse value in force is 2h (Warehouse)." in msg and "withheld unless you tick the override" in msg
    assert plan["override_label"] == ("I accept that a 1h cap would have cancelled 27 completed statements: "
                                      "generate it")
    ok = tighten_timeout_plan("WH_TRXS_TRANSFORM", 7200.0, "WAREHOUSE", impact=_TRXS_27, override=True)
    assert ok["stmt"] == "ALTER WAREHOUSE WH_TRXS_TRANSFORM SET STATEMENT_TIMEOUT_IN_SECONDS = 3600;"
    assert ok["undo"] == "ALTER WAREHOUSE WH_TRXS_TRANSFORM SET STATEMENT_TIMEOUT_IN_SECONDS = 7200;"
    assert ok["level"] == "warning" and "Override ticked" in ok["message"]
    one = tighten_timeout_plan("WH_X", 21600.0, "ACCOUNT", impact=TimeoutImpact(True, 1, None, 3700.0))
    assert "would have cancelled 1 completed statement in the last 30 days (longest 1h 1m)." in one["message"]
    assert "execution time alone" not in one["message"]                      # no exec count: no claim about it
    assert "(Account)" in one["message"]


def test_an_unknown_impact_is_never_read_as_none():
    failed = TimeoutImpact(ok=False, days=30, error="Statement reached its timeout")
    plan = tighten_timeout_plan("WH_TRXS_TRANSFORM", 7200.0, "WAREHOUSE", impact=failed)
    assert plan["level"] == "warning" and plan["stmt"] == "" and plan["override_needed"]
    assert "could not be read" in plan["message"] and "not known (not zero)" in plan["message"]
    assert "none of them" not in plan["message"] and "would have cancelled" not in plan["message"]
    assert plan["override_label"] == "Generate the 1h cap without knowing what it would cancel"


def test_a_verified_zero_impact_generates_the_alter_with_an_info_note():
    plan = tighten_timeout_plan("WH_ALFA_LOAD", 21600.0, "ACCOUNT",
                                impact=TimeoutImpact(True, 0, 0, 2140.0))
    assert plan["level"] == "info" and not plan["override_needed"]
    assert plan["stmt"] == "ALTER WAREHOUSE WH_ALFA_LOAD SET STATEMENT_TIMEOUT_IN_SECONDS = 3600;"
    assert plan["message"].startswith("No completed statement on WH_ALFA_LOAD ran longer than 1h in the last "
                                      "30 days (longest 35m 40s), so the cap would have cancelled none of them.")
    idle = tighten_timeout_plan("WH_IDLE", 21600.0, "ACCOUNT", impact=TimeoutImpact(True, 0, 0, None))
    assert "30 days, so" in idle["message"] and "longest" not in idle["message"]


def test_parse_timeout_impact():
    row = pd.DataFrame([{"completed_runs": 5306096, "over_target_runs": 27, "exec_over_target_runs": 19,
                         "max_elapsed_sec": 9245.0}])
    assert parse_timeout_impact(row, ok=True) == _TRXS_27
    up = pd.DataFrame([{"OVER_TARGET_RUNS": 0, "EXEC_OVER_TARGET_RUNS": 0, "MAX_ELAPSED_SEC": None}])
    assert parse_timeout_impact(up, ok=True, days=7) == TimeoutImpact(True, 0, 0, None, 7)
    # failed / empty / NULL count: unknown, never a zero
    assert parse_timeout_impact(row, ok=False, error="boom") == TimeoutImpact(False, days=30, error="boom")
    assert not parse_timeout_impact(pd.DataFrame(), ok=True).ok
    assert not parse_timeout_impact(None, ok=True).ok
    assert not parse_timeout_impact(pd.DataFrame([{"OVER_TARGET_RUNS": None}]), ok=True).ok
    assert parse_timeout_impact(pd.DataFrame([{"OVER_TARGET_RUNS": 3}]), ok=True).exec_over_target is None


def test_alert_drawer_timeout_lever_reads_before_it_writes():
    al = read("app/ui/pages/alerts.py")
    assert '"Statement timeout 1h",' in al                    # the 1.52.2 radio identity is unchanged
    assert "remediation.statement_timeout_fix(wh_inline, 3600)" not in al      # the blind SET is gone
    branch = al.split('elif fix_kind.startswith("Statement"):', 1)[1].split("\n                            else:", 1)[0]
    # v4.603 (#33 D1) moved this lock: the reads + plan moved out of the drawer into _stmt_timeout_lever
    # (render-tested below), which the Statement branch calls and whose plan it executes
    assert "_cl_plan = _stmt_timeout_lever(wh_inline, event_id)" in branch and 'stmt_cl = _cl_plan["stmt"]' in branch
    lever = _body(al, "_stmt_timeout_lever")
    assert "run(ops_sql.warehouse_stmt_timeout_sql(wh_inline)" in lever and "probe=True" in lever
    # review C13 (this lock was 'tier="metadata"'): the guard gates an EXECUTABLE ALTER + a ledger booking,
    # so it reads on the 30 s live tier -- the 4 h metadata entry could hold a value a DBA tightened
    # outside the app since, and the plan would then loosen it
    show = lever.split("_to_res = run(", 1)[1].split(")\n", 1)[0]
    assert 'tier="live"' in show and 'tier="metadata"' not in lever and "max_rows=0" in show
    from app.core.query import CACHE_TTLS
    assert CACHE_TTLS["live"] <= 30
    # D1: the impact read runs only when the SET would tighten, and before the plan that weighs it
    assert "if stmt_timeout.timeout_would_tighten(_to_cur, _STMT_LEVER_TARGET_S):" in lever
    assert "run(ops_sql.warehouse_timeout_impact(wh_inline, _STMT_LEVER_TARGET_S, _days)" in lever
    assert (lever.index("warehouse_stmt_timeout_sql") < lever.index("warehouse_timeout_impact")
            < lever.index("tighten_timeout_plan"))
    assert 'empty_state("unavailable"' in lever and "detail=_impact.error" in lever
    assert "ACCOUNT_USAGE" not in lever                                  # the builder carries the literal
    # one shared warning/info render for both guarded levers, so the raw st.info ceiling holds
    assert al.count('st.info(plan["message"])') == 1 and 'st.info(_cl_plan["message"])' not in al
    assert al.count("_plan_notice(") == 3                                 # def + the two guarded levers
    assert len(re.findall(r"st\.(?:info|success)\(", al)) <= 9
    # the r34 auto-suspend guard and its V157 note wiring are untouched
    assert "_cl_plan = remediation.tighten_suspend_plan(wh_inline, _cl_cur, _cl_known)" in al
    assert 'STATEMENT_TIMEOUT" if fix_kind.startswith("Statement")' in al


class _LeverSt:
    """The slice of streamlit _stmt_timeout_lever touches, recording what it renders IN SCREEN ORDER: an
    ``empty()`` placeholder holds its slot, and whatever is written inside ``with slot.container():`` lands
    there (so a notice filled after the checkbox still reads above it, as it does on the page).

    ``state`` (review R1-4): None = a stateless checkbox that returns ``tick``; a dict = Streamlit's keyed
    widget state (1.52.2 identifies a keyed checkbox by its key alone, so a tick survives a label change),
    shared across renders -- ``tick_last()`` ticks the checkbox the last render showed."""

    def __init__(self, tick: bool = False, state: dict | None = None):
        self.calls: list[tuple[str, str]] = []
        self.tick = tick
        self.state = state
        self.keys: list[str] = []
        self._slot: int | None = None

    def _put(self, kind: str, text: str) -> None:
        if self._slot is not None:
            self.calls[self._slot] = (kind, text)
        else:
            self.calls.append((kind, text))

    def warning(self, text, *_a, **_k):
        self._put("warning", str(text))

    def info(self, text, *_a, **_k):
        self._put("info", str(text))

    def checkbox(self, label, *_a, key: str = "", **_k):
        self.calls.append(("checkbox", str(label)))
        self.keys.append(key)
        return self.tick if self.state is None else bool(self.state.get(key, False))

    def empty(self):
        fake, idx = self, len(self.calls)
        self.calls.append(("empty", ""))

        class _Slot:
            def container(self):
                return self

            def __enter__(self):
                fake._slot = idx
                return self

            def __exit__(self, *_exc):
                fake._slot = None
                return False

        return _Slot()

    def tick_last(self) -> None:
        assert self.state is not None and self.keys, "no keyed checkbox was rendered"
        self.state[self.keys[-1]] = True


def _lever(monkeypatch, *, value="7200", level="WAREHOUSE", impact=None, tick=False, state=None):
    """Render the drawer's 'Statement timeout 1h' lever with fakes: ``impact`` = the impact read's result
    (a frame, or an Exception for a failed read); ``state`` = keyed checkbox state kept across renders."""
    from types import SimpleNamespace

    from app.ui.pages import alerts
    fake = _LeverSt(tick, state)
    seen: dict = {"sql": [], "empty": []}

    def fake_run(sql, **kw):
        seen["sql"].append((sql, kw))
        if sql.startswith("SHOW PARAMETERS"):
            df = pd.DataFrame([{"key": "STATEMENT_TIMEOUT_IN_SECONDS", "value": value, "level": level}])
            return SimpleNamespace(ok=True, empty=False, df=df, error="", usable=lambda: True)
        if isinstance(impact, Exception):
            return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), error=str(impact),
                                   usable=lambda: False)
        return SimpleNamespace(ok=True, empty=impact.empty, df=impact, error="", usable=lambda: not impact.empty)

    monkeypatch.setattr(alerts, "st", fake)
    monkeypatch.setattr(alerts, "run", fake_run)
    monkeypatch.setattr(alerts, "empty_state", lambda kind, msg, *_a, **k: seen["empty"].append(
        (kind, msg, k.get("detail"))))
    plan = alerts._stmt_timeout_lever("WH_TRXS_TRANSFORM", "0123456789abcdef")
    return plan, fake, seen


_W5B_IMPACT = pd.DataFrame([{"COMPLETED_RUNS": 5306096, "OVER_TARGET_RUNS": 27, "EXEC_OVER_TARGET_RUNS": 19,
                             "MAX_ELAPSED_SEC": 9245.0}])


def test_drawer_lever_withholds_the_alter_when_it_would_cancel_statements(monkeypatch):
    plan, fake, seen = _lever(monkeypatch, impact=_W5B_IMPACT)
    assert plan["stmt"] == "" and plan["level"] == "warning"
    kinds = [k for k, _ in fake.calls]
    assert kinds == ["warning", "checkbox"]                             # the notice, then the explicit override
    assert "would have cancelled 27 completed statements in the last 30 days (longest 2h 34m)" in fake.calls[0][1]
    assert fake.calls[0][1].endswith("The ALTER is withheld unless you tick the override.")   # true while unticked
    assert "Override ticked" not in fake.calls[0][1]
    assert fake.calls[1][1].startswith("I accept that a 1h cap would have cancelled 27")
    # exactly two reads: the live SHOW, then ONE warehouse's 30-day impact (probe, historical tier)
    (show_sql, show_kw), (imp_sql, imp_kw) = seen["sql"]
    assert show_sql.endswith("IN WAREHOUSE WH_TRXS_TRANSFORM") and show_kw["tier"] == "live"
    assert imp_sql == ops_sql.warehouse_timeout_impact("WH_TRXS_TRANSFORM", 3600, 30)
    assert imp_kw["probe"] is True and imp_kw["tier"] == "historical" and "ACCOUNT_USAGE" not in imp_kw["source"]
    assert seen["empty"] == []


def test_drawer_lever_generates_only_after_the_override(monkeypatch):
    plan, fake, _ = _lever(monkeypatch, impact=_W5B_IMPACT, tick=True)
    assert [k for k, _t in fake.calls] == ["warning", "checkbox"]         # the warning stays visible, above
    assert plan["stmt"] == "ALTER WAREHOUSE WH_TRXS_TRANSFORM SET STATEMENT_TIMEOUT_IN_SECONDS = 3600;"
    assert plan["undo"] == "ALTER WAREHOUSE WH_TRXS_TRANSFORM SET STATEMENT_TIMEOUT_IN_SECONDS = 7200;"
    # review R1-5/R1-15: the notice is the FINAL plan's, so once the override is ticked it no longer says the
    # ALTER is withheld directly above the generated ALTER (the 'Override ticked' text was never rendered)
    warning = fake.calls[0][1]
    assert warning.endswith("Override ticked: the ALTER below is generated anyway.") and warning == plan["message"]
    assert "withheld unless you tick" not in warning
    assert "would have cancelled 27 completed statements" in warning       # the impact is still stated


def test_drawer_lever_override_is_tied_to_the_impact_it_acknowledges(monkeypatch):
    """Review R1-4: the override is keyed on the impact it acknowledges. A tick given under one impact (a failed
    read: 'without knowing', or N statements) never carries over as a tick of a DIFFERENT impact's 'I accept
    ...' label -- on Streamlit 1.52.2 a keyed checkbox keeps its value across a label change, and a failed read
    is retried on the very rerun the tick causes."""
    alter = "ALTER WAREHOUSE WH_TRXS_TRANSFORM SET STATEMENT_TIMEOUT_IN_SECONDS = 3600;"
    three = _W5B_IMPACT.assign(OVER_TARGET_RUNS=3)
    failed = RuntimeError("SQL execution canceled")
    for first, then in ((failed, _W5B_IMPACT), (three, _W5B_IMPACT), (_W5B_IMPACT, failed)):
        state: dict = {}
        plan, fake, _ = _lever(monkeypatch, impact=first, state=state)
        assert plan["stmt"] == ""
        fake.tick_last()                                                 # the operator ticks THIS label
        plan, fake, _ = _lever(monkeypatch, impact=first, state=state)
        assert plan["stmt"] == alter                                     # the tick holds while its impact does
        plan, fake, _ = _lever(monkeypatch, impact=then, state=state)
        assert plan["stmt"] == "" and plan["undo"] == "", (first, then)  # a new impact starts unticked
        assert fake.calls[0][1].endswith("The ALTER is withheld unless you tick the override.")
    assert stmt_timeout.override_ack(None) == stmt_timeout.override_ack(TimeoutImpact(ok=False)) == "unknown"
    assert stmt_timeout.override_ack(TimeoutImpact(ok=True, over_target=27)) == "27"
    assert stmt_timeout.override_ack(TimeoutImpact(ok=True, over_target=None)) == "unknown"


def test_drawer_lever_failed_impact_read_is_unavailable_not_none(monkeypatch):
    plan, fake, seen = _lever(monkeypatch, impact=RuntimeError("SQL execution canceled"))
    assert plan["stmt"] == "" and plan["override_needed"]
    assert len(seen["empty"]) == 1
    kind, msg, detail = seen["empty"][0]
    assert kind == "unavailable" and "what a 1h cap would cancel is unknown" in msg
    assert detail == "SQL execution canceled"
    assert fake.calls[0][0] == "warning" and "Impact unknown" in fake.calls[0][1]
    assert not any("none of them" in t for _, t in fake.calls)


def test_drawer_lever_zero_impact_and_no_tightening(monkeypatch):
    zero = pd.DataFrame([{"OVER_TARGET_RUNS": 0, "EXEC_OVER_TARGET_RUNS": 0, "MAX_ELAPSED_SEC": 2140.0}])
    plan, fake, _ = _lever(monkeypatch, value="21600", level="ACCOUNT", impact=zero)
    assert plan["stmt"].endswith("= 3600;") and [k for k, _ in fake.calls] == ["info"]   # no override needed
    # already at/below 1h: no impact read at all, just the info note
    plan, fake, seen = _lever(monkeypatch, value="1800", level="WAREHOUSE", impact=_W5B_IMPACT)
    assert plan["stmt"] == "" and len(seen["sql"]) == 1 and [k for k, _ in fake.calls] == ["info"]


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def test_show_builders_share_admins_cache_entry_and_quote_the_rest():
    admin = read("app/ui/pages/admin.py")
    assert ("f\"SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE {APP_WAREHOUSE}\"" in admin)
    assert (ops_sql.warehouse_stmt_timeout_sql(config.APP_WAREHOUSE)
            == f"SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE {config.APP_WAREHOUSE}")
    assert ops_sql.warehouse_stmt_timeout_sql("wh x").endswith('IN WAREHOUSE "wh x"')
    assert ops_sql.warehouse_stmt_timeout_sql('A"B').endswith('IN WAREHOUSE "A""B"')
    with pytest.raises(ValueError):
        ops_sql.warehouse_stmt_timeout_sql("  ")
    assert ops_sql.account_stmt_timeout_sql() == "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN ACCOUNT"


def test_timeout_tail_shape():
    import sqlglot

    sql = ops_sql.warehouse_timeout_tail(30, "ALL")
    assert ops_sql.warehouse_timeout_tail(400, "ALL").count("DATEADD('day', -90, CURRENT_TIMESTAMP())") == 1
    for s in CAP_LADDER_S:
        assert f"TOTAL_ELAPSED_TIME > {s * 1000}) AS RUNS_OVER_{s}" in sql and f"q.RUNS_OVER_{s}" in sql
    assert "EXECUTION_STATUS <> 'SUCCESS'" in sql and "EXECUTION_STATUS = 'FAIL'" not in sql
    assert "ILIKE '%statement or warehouse timeout%'" in sql
    assert sql.count("COMPANY_FOR_WAREHOUSE") == 1                     # the outer label only, per warehouse
    scoped = ops_sql.warehouse_timeout_tail(30, "ALFA")
    outer = scoped.split("\nFROM q\n", 1)[1]
    assert "COMPANY_FOR_WAREHOUSE(q.WAREHOUSE_NAME) = 'ALFA'" in outer   # filtered on the OUTER query
    assert "q.*" not in sql and "LIMIT" not in sql
    assert "SUM(q.TIMEOUT_CANCELLED_RUNS) OVER () AS TIMEOUT_CANCELLED_TOTAL" in sql
    # v4.603 (#33 D5) moved this lock: + the fired-ceiling range, parsed with the W5b probe's regex (thousands
    # commas stripped) under the SAME predicate the count uses
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == [
        "WAREHOUSE_NAME", "COMPANY", "COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC",
        "TIMEOUT_CANCELLED_RUNS", "TIMEOUT_FIRED_MIN_SEC", "TIMEOUT_FIRED_MAX_SEC", "TIMEOUT_CANCELLED_TOTAL",
        *[f"RUNS_OVER_{s}" for s in CAP_LADDER_S]]
    fired = ("IFF(EXECUTION_STATUS <> 'SUCCESS' AND ERROR_MESSAGE ILIKE '%statement or warehouse timeout%', "
             "TRY_TO_NUMBER(REPLACE(REGEXP_SUBSTR(ERROR_MESSAGE, 'timeout of ([0-9,]+) second', 1, 1, 'e', 1), "
             "',', '')), NULL)")
    assert f"MIN({fired}) AS TIMEOUT_FIRED_MIN_SEC" in sql and f"MAX({fired}) AS TIMEOUT_FIRED_MAX_SEC" in sql
    assert ("COUNT_IF(EXECUTION_STATUS <> 'SUCCESS' AND ERROR_MESSAGE ILIKE '%statement or warehouse timeout%') "
            "AS TIMEOUT_CANCELLED_RUNS") in sql
    assert "::" not in sql
    # the loader-robustness rule for this file still holds
    src = read("app/data/ops_sql.py")
    assert "EXECUTION_STATUS = 'FAIL'" not in src and src.count("<> 'SUCCESS'") >= 2


def test_timeout_impact_builder_shape():
    """#33 D1: one warehouse, completed statements only, one row; elapsed AND execution-time counts."""
    import sqlglot

    sql = ops_sql.warehouse_timeout_impact("WH_TRXS_TRANSFORM", 3600, 30)
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == [
        "COMPLETED_RUNS", "OVER_TARGET_RUNS", "EXEC_OVER_TARGET_RUNS", "MAX_ELAPSED_SEC"]
    assert "COUNT_IF(EXECUTION_STATUS = 'SUCCESS' AND TOTAL_ELAPSED_TIME > 3600000) AS OVER_TARGET_RUNS" in sql
    assert "COUNT_IF(EXECUTION_STATUS = 'SUCCESS' AND EXECUTION_TIME > 3600000)     AS EXEC_OVER_TARGET_RUNS" in sql
    assert "UPPER(WAREHOUSE_NAME) = 'WH_TRXS_TRANSFORM'" in sql
    assert "DATEADD('day', -30, CURRENT_TIMESTAMP())" in sql
    assert "GROUP BY" not in sql and "LIMIT" not in sql and "COMPANY_FOR_WAREHOUSE" not in sql
    assert "DATEADD('day', -90," in ops_sql.warehouse_timeout_impact("WH_A", 3600, 400)       # clamped
    assert "> 900000)" in ops_sql.warehouse_timeout_impact("wh_a", 900, 7)
    assert "= 'WH_A'" in ops_sql.warehouse_timeout_impact("wh_a", 900, 7)                      # upper-cased
    for bad in ("", "WH'; DROP TABLE X; --", "A.B"):
        with pytest.raises(ValueError):
            ops_sql.warehouse_timeout_impact(bad)


def test_canary_registration():
    reg = dict(canary.CANARIES)
    assert reg["ops.warehouse_timeout_tail"]() == ops_sql.warehouse_timeout_tail(1, "ALFA")
    assert reg["ops.warehouse_timeout_impact"]() == ops_sql.warehouse_timeout_impact("WH_ALFA_ADMIN", 3600, 1)
    names = [n for n, _ in canary.CANARIES]
    assert not any("stmt_timeout" in n for n in names)                 # SHOW cannot be EXPLAINed
    assert all("SHOW PARAMETERS" not in fn() for _, fn in canary.CANARIES if _.startswith("ops."))


# ---------------------------------------------------------------------------
# page wiring
# ---------------------------------------------------------------------------

def test_panel_wiring_and_gating():
    ops = page_source("operations")
    sizing = _body(ops, "_wh_sizing_efficiency")
    assert "_stmt_timeout_posture_panel(company, days)" in sizing
    assert sizing.index("result_caption(_hh)") < sizing.index("_stmt_timeout_posture_panel(company, days)") \
        < sizing.index("_adaptive_candidacy_panel(company, days, bounds=bounds)")
    body = _body(ops, "_stmt_timeout_posture_panel")
    assert body.index('key="ops_wh_timeout_load"') < body.index("run(")     # every read behind the toggle
    for banned in ("execute_statement", "confirm_gate", "write_gate_open", "REMEDIATION_LOG", "ACCOUNT_USAGE",
                   "methodology_note", "st.info(", "st.success(", ") or {}", "st.selectbox", "st.subheader"):
        assert banned not in body, banned
    assert "st.code(script" in body and body.count("probe=True") == 2
    assert "stmt_timeout.fix_script(posture, tail_days, tail_ok=tail.ok)" in body   # a failed tail says so
    assert 'key="jump_wh"' in body and 'tier="metadata"' in body          # shares the SHOW WAREHOUSES cache
    assert "run_batch" not in body                                      # plain run() per SHOW (D4)
    assert 'empty_state("clean"' in body and 'summ["read"] and not summ["uncapped"]' in body
    assert 'TIMEOUT_CANCELLED_TOTAL"].iloc[0]' in body                  # the SQL total, never a frame sum
    # the page-wide counts this panel must not move
    assert read("app/ui/pages/operations.py").count("ACCOUNT_USAGE") == 42
    assert "stmt_timeout," in ops.split("from app.logic import (", 1)[1].split(")", 1)[0]


def test_admin_parity_and_untouched():
    admin = read("app/ui/pages/admin.py")
    m = re.search(r"^_SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S = ([\d_]+)$", admin, re.M)
    assert m and int(m.group(1).replace("_", "")) == stmt_timeout.SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S == 172_800
    # review R1-8 moved this lock ("stmt_timeout." not in admin), and review R2-5 again: Admin's ceiling tile
    # shows the EFFECTIVE ceiling (the lower non-zero of the warehouse and account values) through the shared
    # pure helpers, and only those; it reads the account value with the posture panel's builder (one cache
    # entry), while its warehouse SHOW stays its own literal (the shared cache entry, locked above)
    assert set(re.findall(r"stmt_timeout\.(\w+)", admin)) == {
        "parse_timeout_row", "enforced_s", "derive_account_timeout", "effective_timeout_s"}
    assert "ops_sql.warehouse_stmt_timeout_sql" not in admin
    assert admin.count("ops_sql.account_stmt_timeout_sql()") == 1


def test_duration_and_count_naming():
    from app.ui.components import _COUNT_SUFFIXES, _duration_unit_for_column
    for col in POSTURE_COLUMNS:
        if col.endswith("_SEC"):
            assert _duration_unit_for_column(col) == "s", col
        if col.endswith("RUNS"):
            assert col.endswith(_COUNT_SUFFIXES) and _duration_unit_for_column(col) is None, col
    assert _duration_unit_for_column("TIMEOUT_CANCELLED_RUNS") is None


def test_status_colors():
    from app.ui.status_colors import status_css
    assert status_css("STATUS", STATUS_UNCAPPED) and status_css("STATUS", STATUS_CAPPED)
    assert status_css("STATUS", STATUS_UNCAPPED) != status_css("STATUS", STATUS_CAPPED)
    assert status_css("STATUS", STATUS_NOT_VISIBLE) == ""
    assert status_css("STATUS", STATUS_MANAGED) == ""                    # neutral: nothing to fix there


def test_emergency_timeout_lever_says_what_zero_enforces():
    """#33 D3: 0 is Snowflake's 7-day maximum, not 'no cap' (stmt_timeout.enforced_s), and the lower non-zero
    of it and the session/account value still fires."""
    ops = read("app/ui/pages/operations.py")
    assert "0 = no cap" not in ops
    assert ('st.number_input("Timeout seconds (0 = Snowflake\'s 7-day maximum; a lower "\n'
            '                                       "session/account value still applies)", 0, 604800, 3600,') in ops
    doc = remediation.statement_timeout_fix.__doc__ or ""
    assert "disables the cap" not in doc and "7-day maximum" in doc and "session/account value" in doc
    assert stmt_timeout.enforced_s(0) == stmt_timeout.SNOWFLAKE_MAX_STMT_TIMEOUT_S == 604_800
    assert remediation.statement_timeout_fix("WH_A", 0).endswith("= 0;")          # the SQL itself is unchanged


def test_posture_panel_ties_timed_out_to_the_ceiling_that_fired():
    body = _body(page_source("operations"), "_stmt_timeout_posture_panel")
    assert "posture[stmt_timeout.DISPLAY_COLUMNS]" in body and "POSTURE_COLUMNS[:10]" not in body
    assert '"TIMEOUT_FIRED": st.column_config.TextColumn(\n                "Fired at"' in body
    assert "caps that already fired" not in body                        # the old KPI help
    assert "at whichever ceiling was lowest for that statement" in body
    assert "for _note in stmt_timeout.status_notes(summ):" in body
    assert "stmt_timeout.timeout_posture(names, params, account_s, tail_df, not_visible, managed)" in body
