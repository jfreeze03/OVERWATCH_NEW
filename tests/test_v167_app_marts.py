"""v4.609 app half of V167 (cluster marts): R1-016 (the AI coverage stamp), PATTERN-RESTAMP + R2-010 (the pattern
cap and captions), R2-052 (Central day keys on the live Cortex Code legs), R2-014 (the task-graph caption) and the
C10 session-pad caption.

Every behaviour that claims V167 is gated on ``has_migration(167, page)`` (house law 12) and the pre-apply path
renders exactly as before; the builders' new keyword arguments default to today's SQL byte-for-byte. The stamped
coverage gate is EXECUTED (a minimal shim, sqlite) on a fully loaded fact whose Code first use falls mid-window,
the round-1 R1-016 repro: unstamped it blanks 180d / 365d / Current year, stamped it answers.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.data import canary, cortex_sql, mart27_sql
from app.logic import cost_coverage
from app.logic.formulas import account_today
from tests._source import read
from tests.test_probe_absence_split import _failed, _ok, _patch

_STAMP_LOCK = "LEAST(COALESCE(st.CF, f.FIRST_DAY), COALESCE(f.FIRST_DAY, st.CF))"
_STAMP_SRC = "FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE"
_AUG = (date(2026, 8, 1), date(2026, 9, 1))

# (stamped render, the same call WITHOUT the keyword): the default must be today's SQL
_AI_BUILDERS = {
    "ai_code_daily": (lambda st: mart27_sql.ai_code_daily(180, "ALL", stamped=st),
                      lambda: mart27_sql.ai_code_daily(180, "ALL")),
    "ai_code_daily_lm": (lambda st: mart27_sql.ai_code_daily(31, "ALFA", bounds=_AUG, stamped=st),
                         lambda: mart27_sql.ai_code_daily(31, "ALFA", bounds=_AUG)),
    "ai_code_user_rollup": (lambda st: mart27_sql.ai_code_user_rollup(365, "Trexis", stamped=st),
                            lambda: mart27_sql.ai_code_user_rollup(365, "Trexis")),
    "ai_code_user_daily": (lambda st: mart27_sql.ai_code_user_daily("ALL", stamped=st),
                           lambda: mart27_sql.ai_code_user_daily("ALL")),
    "ai_costs_by_model": (lambda st: mart27_sql.ai_costs_by_model(365, stamped=st),
                          lambda: mart27_sql.ai_costs_by_model(365)),
}


# ============================================================================================ builders ====

@pytest.mark.parametrize("name", sorted(_AI_BUILDERS))
def test_r1_016_stamped_false_is_todays_sql_and_true_reads_the_v167_stamp(name):
    render, default = _AI_BUILDERS[name]
    plain = render(False)
    assert plain == default()                 # the canary and every pre-V167 call site are unchanged
    assert "COVERAGE_FROM" not in plain and "SOURCE_FRESHNESS_STATE" not in plain
    stamped = render(True)
    assert _STAMP_LOCK in stamped
    # the Code gates read the raw stamp; the all-source gate floors it at the Functions view horizon
    assert re.search(r"MAX\(s2?\.COVERAGE_FROM\)(, DATE\('2026-01-05'\)\))? AS CF", stamped)
    assert _STAMP_SRC in stamped and "SOURCE_NAME = 'FACT_AI_USAGE_DAILY'" in stamped
    import sqlglot
    sqlglot.parse_one(stamped, read="snowflake")


def test_r1_016_ai_costs_by_model_stamp_gates_all_sources():
    sql = mart27_sql.ai_costs_by_model(365, stamped=True)
    assert "SELECT MIN(a2.DAY) AS FIRST_DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY a2" in sql
    assert "SOURCE <> 'Functions'" not in sql                   # Code + Functions, as before
    assert "<= DATEADD('day', -365 + 1, CURRENT_DATE())" in sql
    # review (rebuild): the canonical Functions view holds data only from 2026-01-05 (V146), so the loader's
    # stamp cannot vouch for Functions before it -- the all-source gate floors the stamp there (NULL stays NULL)
    assert date(2026, 1, 5) == mart27_sql.AI_FUNCTIONS_VIEW_FROM
    assert "SELECT GREATEST(MAX(s2.COVERAGE_FROM), DATE('2026-01-05')) AS CF FROM " in sql
    # the Code-only gates keep the raw stamp: the Code views reload their whole retention
    assert "GREATEST(" not in mart27_sql.ai_code_daily(365, "ALL", stamped=True)


def test_ai_fact_coverage_renders_each_gates_own_reach_text():
    """review (R1-016 reach): the help names the reach the gate tests -- the reach builder embeds the SAME text the
    Code gates (_ai_code_coverage_cte) and the all-source gate (ai_costs_by_model) compare, not the stamp alone."""
    sql = mart27_sql.ai_fact_coverage()
    assert "WITH " + mart27_sql._ai_code_coverage_cte(True) in sql
    assert "cov.FIRST_DAY AS CODE_REACH" in sql
    all_first = mart27_sql._ai_all_first_day(True)
    assert f"{all_first} AS ALL_REACH" in sql
    assert f"AND {all_first}\n      <= " in mart27_sql.ai_costs_by_model(365, stamped=True)
    assert f"AND {mart27_sql._ai_all_first_day(False)}\n      <= " in mart27_sql.ai_costs_by_model(365)
    assert "DATE(MAX(s3.SNAPSHOT_TS)) AS LOADED_ON" in sql and "MAX(s3.COVERAGE_FROM) AS COVERAGE_FROM" in sql
    assert "ACCOUNT_USAGE" not in sql
    import sqlglot
    sqlglot.parse_one(sql, read="snowflake")


def test_fact_coverage_from_is_a_core_point_read_with_validated_names():
    sql = mart27_sql.fact_coverage_from()
    assert "FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE" in sql
    assert "WHERE SOURCE_NAME IN ('FACT_AI_USAGE_DAILY', 'MART_PATTERN_COST_DAILY')" in sql
    assert "ACCOUNT_USAGE" not in sql
    assert "IN ('FACT_AI_USAGE_DAILY')" in mart27_sql.fact_coverage_from("FACT_AI_USAGE_DAILY")
    for bad in ("x'; DROP", "lower_case", "", ()):
        with pytest.raises(ValueError):
            mart27_sql.fact_coverage_from(bad)
    import sqlglot
    sqlglot.parse_one(sql, read="snowflake")


def test_fact_coverage_canary_is_registered_and_skipped_until_v167():
    """correction 9: the canary entry reads the V167 column and the app deploys first; a missing column is
    drift (missing_column), never a declared gap, so the runner skips the entry until has_migration(167)."""
    reg = dict(canary.CANARIES)
    assert reg["mart27.fact_coverage_from"]() == mart27_sql.fact_coverage_from()
    assert reg["mart27.ai_fact_coverage"]() == mart27_sql.ai_fact_coverage()
    assert canary.MIGRATION_GATED == {"mart27.fact_coverage_from": 167, "mart27.ai_fact_coverage": 167}
    for name in canary.MIGRATION_GATED:
        assert name not in canary.EXPECTED_GAPS
        assert canary.gated_out(name, frozenset(range(1, 167)))
        assert not canary.gated_out(name, frozenset(range(1, 168)))
    assert not canary.gated_out("mart27.pattern_cost", frozenset())   # an ungated entry always runs
    # the AI builders' canaries stay bare (unstamped): they compile before V167 too
    assert reg["mart27.ai_costs_by_model"]() == mart27_sql.ai_costs_by_model(2)


# ------------------------------------------------------- executed: the round-1 R1-016 repro, stamped ----

_TODAY = date(2026, 10, 1)


def _dateadd(unit: str, n: int, d: str) -> str:
    assert unit == "day"
    return (date.fromisoformat(d[:10]) + timedelta(days=int(n))).isoformat()


def _least(*a):
    return None if any(v is None for v in a) else min(a)


def _greatest(*a):          # Snowflake GREATEST: NULL when any argument is NULL
    return None if any(v is None for v in a) else max(a)


def _lite(sql: str) -> str:
    out = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    out = out.replace("CURRENT_DATE()", f"'{_TODAY.isoformat()}'").replace("ANY_VALUE(", "MAX(")
    out = re.sub(r"--[^\n]*", "", out)
    for tok in ("::", "QUALIFY", "IFF(", "CONVERT_TIMEZONE"):
        assert tok not in out, tok
    return out


_FACT_ROWS = [("2026-06-15", "U1", "Snowsight", "n/a", 5.0),        # Code first use mid-year
              ("2026-09-20", "U2", "CLI", "n/a", 7.0),
              ("2026-01-10", "ACCOUNT", "Functions", "llama", 3.0)]  # Functions started earlier


def _ai_db(stamp: str | None, rows: list | None = None, loaded_on: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("LEAST", -1, _least)
    con.create_function("GREATEST", -1, _greatest)
    con.execute("CREATE TABLE FACT_AI_USAGE_DAILY (DAY TEXT, USER_NAME TEXT, SOURCE TEXT, MODEL_NAME TEXT, "
                "EMAIL TEXT, FIRST_TS TEXT, LAST_TS TEXT, REQUESTS INT, TOKENS INT, CREDITS REAL)")
    con.executemany("INSERT INTO FACT_AI_USAGE_DAILY VALUES (?, ?, ?, ?, NULL, NULL, NULL, 1, 10, ?)",
                    _FACT_ROWS if rows is None else rows)
    con.execute("CREATE TABLE SOURCE_FRESHNESS_STATE (SOURCE_NAME TEXT, COVERAGE_FROM TEXT, SNAPSHOT_TS TEXT)")
    con.execute("INSERT INTO SOURCE_FRESHNESS_STATE VALUES ('FACT_AI_USAGE_DAILY', ?, ?)",
                (stamp, f"{loaded_on} 06:47:12" if loaded_on else None))
    con.execute("CREATE TABLE USERS (NAME TEXT, FIRST_NAME TEXT, LAST_NAME TEXT, DELETED_ON TEXT)")
    return con


def _credits(con: sqlite3.Connection, sql: str, col: str) -> float:
    df = pd.read_sql_query(_lite(sql), con)
    return round(float(df[col].sum()), 4) if not df.empty else 0.0


_YEAR = (date(2026, 1, 1), date(2026, 10, 2))


@pytest.mark.parametrize(("stamp", "answered"), [
    ((_TODAY - timedelta(days=364)).isoformat(), True),          # after the owner's DAILY 365 reload
    (None, False),                                               # no stamp: today's MIN(DAY) gate
    ((_TODAY - timedelta(days=2)).isoformat(), False),           # only the daily d=3 run so far
])
def test_r1_016_stamped_gate_answers_a_window_that_starts_before_first_use(stamp, answered):
    con = _ai_db(stamp)
    for days in (180, 365):
        got = _credits(con, mart27_sql.ai_code_daily(days, "ALL", stamped=True), "TOTAL_CREDITS")
        assert got == (12.0 if answered else 0.0), (stamp, days)
        assert _credits(con, mart27_sql.ai_code_daily(days, "ALL"), "TOTAL_CREDITS") == 0.0   # pre-V167
    year = _credits(con, mart27_sql.ai_code_daily(274, "ALL", bounds=_YEAR, stamped=True), "TOTAL_CREDITS")
    assert year == (12.0 if answered else 0.0)
    # Code + Functions: a window that starts on or after the Functions view horizon (2026-01-05) but before the
    # account's first AI use is answered by the stamp; one that starts before the horizon still needs ROWS
    # (the stamp cannot vouch for Functions there -- see the horizon test below)
    con.execute("DELETE FROM FACT_AI_USAGE_DAILY WHERE SOURCE = 'Functions'")      # first Functions use: Mar 1
    con.execute("INSERT INTO FACT_AI_USAGE_DAILY VALUES ('2026-03-01', 'ACCOUNT', 'Functions', 'llama', "
                "NULL, NULL, NULL, 1, 10, 3.0)")
    model = _credits(con, mart27_sql.ai_costs_by_model(260, stamped=True), "CREDITS")
    assert model == (15.0 if answered else 0.0)                  # Code + Functions
    assert _credits(con, mart27_sql.ai_costs_by_model(260), "CREDITS") == 0.0
    assert _credits(con, mart27_sql.ai_costs_by_model(365, stamped=True), "CREDITS") == 0.0
    rollup = _credits(con, mart27_sql.ai_code_user_rollup(365, "ALL", stamped=True), "TOTAL_CREDITS")
    assert rollup == (12.0 if answered else 0.0)


def test_r1_016_all_source_gate_never_trusts_the_stamp_before_the_functions_view_horizon():
    """review (rebuild): teardown / rebuild drop FACT_AI_USAGE_DAILY and the backfill's DAILY 365 stamps
    COVERAGE_FROM = today - 364, but the canonical CORTEX_AI_FUNCTIONS_USAGE_HISTORY holds data only from
    2026-01-05 (V146; the older Functions history came from the frozen view no loader reads). A 365d / Current-year
    Unit costs 'AI spend' must not pass on the stamp and sum a year with a quarter of Functions spend missing: it
    keeps the pre-V167 MIN(DAY) test there and falls back to the honestly labelled Functions-only read."""
    rebuilt = [("2026-06-15", "U1", "Snowsight", "n/a", 5.0), ("2026-09-20", "U2", "CLI", "n/a", 7.0),
               ("2026-01-05", "ACCOUNT", "Functions", "llama", 3.0)]      # the canonical view's first day
    con = _ai_db((_TODAY - timedelta(days=364)).isoformat(), rebuilt)
    for model_sql in (mart27_sql.ai_costs_by_model(365, stamped=True),
                      mart27_sql.ai_costs_by_model(274, bounds=_YEAR, stamped=True)):
        assert _credits(con, model_sql, "CREDITS") == 0.0
    # the Code views reload their whole retention: the Code-only readers still answer the year from the stamp
    assert _credits(con, mart27_sql.ai_code_daily(365, "ALL", stamped=True), "TOTAL_CREDITS") == 12.0
    # from the horizon on, the stamp vouches for both arms
    assert _credits(con, mart27_sql.ai_costs_by_model(269, stamped=True), "CREDITS") == 15.0   # from 2026-01-06
    # a non-rebuilt fact that kept frozen-view Functions rows answers as before V167 (rows, not the stamp)
    con.execute("INSERT INTO FACT_AI_USAGE_DAILY VALUES ('2025-10-02', 'ACCOUNT', 'Functions', 'llama', "
                "NULL, NULL, NULL, 1, 10, 2.0)")
    assert _credits(con, mart27_sql.ai_costs_by_model(365, stamped=True), "CREDITS") == 17.0
    assert _credits(con, mart27_sql.ai_costs_by_model(365), "CREDITS") == 17.0


_GRID_STARTS = (date(2025, 10, 2), date(2025, 12, 1), date(2026, 1, 5), date(2026, 1, 6), date(2026, 2, 1),
                date(2026, 6, 15), date(2026, 6, 16), date(2026, 9, 29), _TODAY)


@pytest.mark.parametrize("stamp", [None, "2025-10-02", "2026-03-01", "2026-09-29"])
@pytest.mark.parametrize(("code_first", "fn_first"), [("2026-06-15", "2026-01-10"), ("2025-11-20", None),
                                                      (None, "2025-12-01"), (None, None)])
def test_ai_fact_coverage_reach_is_the_reach_each_gate_tests(stamp, code_first, fn_first):
    """Executed parity: for every window start, a stamped Code read answers iff CODE_REACH <= start and the
    all-source read answers iff ALL_REACH <= start (a usage row on today keeps every window non-empty, so an
    empty read IS a failed gate)."""
    rows = [(_TODAY.isoformat(), "U9", "CLI", "n/a", 1.0), (_TODAY.isoformat(), "ACCOUNT", "Functions", "m", 1.0)]
    rows += [(code_first, "U1", "Snowsight", "n/a", 1.0)] if code_first else []
    rows += [(fn_first, "ACCOUNT", "Functions", "m", 1.0)] if fn_first else []
    con = _ai_db(stamp, rows)
    cov = pd.read_sql_query(_lite(mart27_sql.ai_fact_coverage()), con)
    assert len(cov) == 1
    code_reach, all_reach = cov["CODE_REACH"].iloc[0], cov["ALL_REACH"].iloc[0]
    assert cov["COVERAGE_FROM"].iloc[0] == stamp
    for start in _GRID_STARTS:
        days = (_TODAY - start).days + 1
        code = pd.read_sql_query(_lite(mart27_sql.ai_code_daily(days, "ALL", stamped=True)), con)
        model = pd.read_sql_query(_lite(mart27_sql.ai_costs_by_model(days, stamped=True)), con)
        assert (not code.empty) == (code_reach is not None and code_reach <= start.isoformat()), (start, code_reach)
        assert (not model.empty) == (all_reach is not None and all_reach <= start.isoformat()), (start, all_reach)


def test_ai_fact_coverage_names_the_last_both_arm_load_day():
    con = _ai_db("2025-10-02", loaded_on="2026-09-23")
    cov = pd.read_sql_query(_lite(mart27_sql.ai_fact_coverage()), con)
    assert cov["LOADED_ON"].iloc[0] == "2026-09-23"
    con = _ai_db(None)                                           # no stamp row content yet
    cov = pd.read_sql_query(_lite(mart27_sql.ai_fact_coverage()), con)
    assert cov["LOADED_ON"].iloc[0] is None and cov["COVERAGE_FROM"].iloc[0] is None
    assert cov["CODE_REACH"].iloc[0] == "2026-06-15"             # no stamp: the fact's first Code day


def test_r1_016_a_loaded_window_with_no_usage_is_answered_not_blanked():
    con = _ai_db((_TODAY - timedelta(days=364)).isoformat())
    con.execute("DELETE FROM FACT_AI_USAGE_DAILY")
    df = pd.read_sql_query(_lite(mart27_sql.ai_code_daily(180, "ALL", stamped=True)), con)
    assert df.empty          # zero rows, but the gate passed: the loader covered the window
    gate = pd.read_sql_query(_lite("WITH " + mart27_sql._ai_code_coverage_cte(True)
                                   + " SELECT FIRST_DAY FROM cov"), con)
    assert gate["FIRST_DAY"].iloc[0] == (_TODAY - timedelta(days=364)).isoformat()


# ---------------------------------------------------------------------------- pattern cap + horizon ----

def test_pattern_cost_cap_follows_the_stamp_between_90_and_365():
    cap = cost_coverage.pattern_cost_cap
    kw = {"floor": 90, "ceiling": 365}
    assert cap(None, _TODAY, **kw) == 90                                  # no stamp: the pre-V167 cap
    assert cap(_TODAY - timedelta(days=3), _TODAY, **kw) == 90            # the daily CALL(3) stamp
    assert cap(_TODAY - timedelta(days=200), _TODAY, **kw) == 200         # a partial reload
    assert cap(_TODAY - timedelta(days=364), _TODAY, **kw) == 364         # CALL(364) day: first day = stamp
    assert cap(_TODAY - timedelta(days=500), _TODAY, **kw) == 365         # never past MAX_MART_WINDOW_DAYS
    # correction 8: a CALL(120) stamps a day AFTER the V120 horizon -- the read still never starts before it
    stamp = _TODAY - timedelta(days=120)
    assert _TODAY - timedelta(days=cap(stamp, _TODAY, **kw)) >= stamp


def test_pattern_clean_from_is_the_deeper_of_v120_and_the_stamp():
    horizon = mart27_sql.PATTERN_COST_RESTAMP_FROM
    assert cost_coverage.pattern_clean_from(None, horizon) == horizon
    assert cost_coverage.pattern_clean_from(date(2026, 9, 28), horizon) == horizon
    assert cost_coverage.pattern_clean_from(date(2025, 10, 2), horizon) == date(2025, 10, 2)


def test_pattern_cost_coverage_from_none_is_todays_sql_and_a_stamp_lifts_the_trailing_cap():
    assert mart27_sql.pattern_cost(365, "ALL", coverage_from=None) == mart27_sql.pattern_cost(365, "ALL")
    today = account_today()
    deep = mart27_sql.pattern_cost(365, "ALL", coverage_from=today - timedelta(days=400))
    assert "WHERE p.DAY >= DATEADD('day', -365, CURRENT_DATE())" in deep and "SUM(p.RUNS) >= 61" in deep
    part = mart27_sql.pattern_cost(365, "ALL", coverage_from=today - timedelta(days=200))
    assert "WHERE p.DAY >= DATEADD('day', -200, CURRENT_DATE())" in part
    # a calendar preset keeps its exact bounds whatever the stamp
    ytd = mart27_sql.pattern_cost(273, "ALL", bounds=_YEAR, coverage_from=today - timedelta(days=400))
    assert "WHERE p.DAY >= '2026-01-01' AND p.DAY < '2026-10-02'" in ytd


# =============================================================================================== pages ====

@pytest.mark.parametrize(("rel", "calls"), [
    ("app/ui/pages/cost_parts/spend.py", 2),
    ("app/ui/pages/cost_parts/ai_chargeback.py", 2),
    ("app/ui/pages/cost_parts/unit_costs.py", 1),
    ("app/ui/pages/security.py", 1),
])
def test_r1_016_every_ai_fact_reader_passes_the_v167_gate(rel, calls):
    src = read(rel)
    reads = re.findall(r"mart27_sql\.(?:ai_code_daily|ai_code_user_rollup|ai_code_user_daily|ai_costs_by_model)"
                       r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", src)
    assert len(reads) == calls, (rel, reads)
    for args in reads:
        assert "stamped=has_migration(167, _PAGE)" in args, (rel, args)
    assert "from app.ui.schema_gate import has_migration" in src


def test_r1_016_help_texts_say_what_the_dash_means():
    """holistic #17 (law 12): the CoCo '—' names reach / freshness only once V167 can turn a covered, fresh, empty
    read into a measured $0.00 (_coco_verified_zero). Before V167 (or while has_migration(167) still answers False)
    every OK-empty read is '—', and an empty read cannot tell 'no usage in the window' from 'not covered'."""
    from app.ui.pages.cost_parts import spend
    src = read("app/ui/pages/cost_parts/spend.py")
    assert "'—' until the fact loads." not in src
    after = spend._coco_dash_help(True, "")
    assert after == ("'—' while FACT_AI_USAGE_DAILY does not reach back to this window's start or is not "
                     "loaded through its end (or could not be read).")
    assert spend._coco_dash_help(True, "The AI fact holds 9 days.") == f"{after} The AI fact holds 9 days."
    before = spend._coco_dash_help(False, "")
    assert before == ("'—' when FACT_AI_USAGE_DAILY returned no Cortex Code rows for this window (no usage in it, "
                      "or the fact does not cover it; the app cannot tell which until V167 is applied) or could "
                      "not be read.")
    assert "reach back" not in before and "loaded through its end" not in before
    assert spend._coco_dash_help(False, "The AI fact holds 9 days.") == before    # no V167 reach to qualify
    assert "_v167 = has_migration(167, _PAGE)" in src and "_coco_dash_help(_v167, _coco_note)" in src
    uc = read("app/ui/pages/cost_parts/unit_costs.py")
    assert "was unavailable on this refresh" not in uc
    assert "does not cover this whole window (or could not be read)" in uc


_COV_COLS = ("CODE_REACH", "ALL_REACH", "COVERAGE_FROM", "LOADED_ON")


def _cov_frame(**vals) -> pd.DataFrame:
    return pd.DataFrame({c: [vals.get(c)] for c in _COV_COLS})


def test_r1_016_spend_reads_the_ai_coverage_only_after_v167(monkeypatch):
    from app.ui.pages.cost_parts import spend
    monkeypatch.setattr(spend, "has_migration", lambda n, _p: False)
    monkeypatch.setattr(spend, "run", lambda *_a, **_k: pytest.fail("no V167 column read before the apply"))
    assert spend._ai_fact_coverage() == {}
    reach, loaded = account_today() - timedelta(days=364), account_today()
    seen = []
    monkeypatch.setattr(spend, "has_migration", lambda n, _p: n <= 167)
    monkeypatch.setattr(spend, "run", lambda sql, **k: seen.append((sql, k["key"])) or _ok(
        _cov_frame(CODE_REACH=reach, ALL_REACH=reach, COVERAGE_FROM=reach, LOADED_ON=loaded)))
    assert spend._ai_fact_coverage() == {"CODE_REACH": reach, "ALL_REACH": reach, "COVERAGE_FROM": reach,
                                         "LOADED_ON": loaded}
    assert seen == [(mart27_sql.ai_fact_coverage(), "coco_ai_reach")]
    monkeypatch.setattr(spend, "run", lambda *_a, **_k: _failed("missing_column"))
    assert spend._ai_fact_coverage() == {}                       # a failed read never guesses
    # review: the stamp-only helpers are gone (one was dead in app code; the tab names the gate's reach now)
    assert not hasattr(spend, "_ai_fact_reach") and not hasattr(spend, "_ai_fact_stamp")


def test_r1_016_coco_zero_needs_the_gates_reach_and_a_fresh_load():
    """review: an OK-but-empty stamped CoCo read is a MEASURED $0.00 only when the gate's own reach covers the
    window start AND the fact's last both-arm DAILY load reaches the window end (the d=3 cadence: on or after
    min(last day, today) - 1). A deep reach alone is not enough: COVERAGE_FROM only moves earlier, and the
    freshness MERGE skips the row whenever either AI arm fails, so an ai_code arm that dies on every run (it
    swallows its own error; V078 recorded exactly that) would otherwise turn an unloaded week into '$0.00'."""
    from app.ui.pages.cost_parts import spend
    today = date(2026, 10, 1)
    start, last = today - timedelta(days=6), today                 # a trailing 7d window
    fresh = {"CODE_REACH": date(2026, 6, 15), "LOADED_ON": today}
    empty, rows = _ok(pd.DataFrame()), _ok(pd.DataFrame({"TOTAL_CREDITS": [1.0]}))
    zero = spend._coco_verified_zero
    assert zero(empty, fresh, start, last, today)                                   # covered + fresh: $0.00
    assert zero(empty, {**fresh, "LOADED_ON": today - timedelta(days=1)}, start, last, today)   # before 06:45
    assert not zero(empty, {**fresh, "LOADED_ON": today - timedelta(days=2)}, start, last, today)   # stale
    assert not zero(empty, {"CODE_REACH": date(2025, 10, 2)}, start, last, today)   # no load day: no claim
    assert not zero(empty, {**fresh, "CODE_REACH": start + timedelta(days=1)}, start, last, today)  # short reach
    assert not zero(empty, {}, start, last, today)                                  # before V167 / failed read
    assert not zero(_failed("timeout"), fresh, start, last, today)
    assert not zero(rows, fresh, start, last, today)                                # rows: the figure stands
    assert not zero(None, fresh, start, last, today)
    # 'Last month' (Aug): loaded through Aug 31 within the cadence, whatever has happened since
    lm = (date(2026, 8, 1), date(2026, 8, 31))
    assert zero(empty, {"CODE_REACH": date(2026, 6, 15), "LOADED_ON": date(2026, 8, 30)}, *lm, today)
    assert not zero(empty, {"CODE_REACH": date(2026, 6, 15), "LOADED_ON": date(2026, 8, 29)}, *lm, today)


def _render_coco_tile(monkeypatch, *, v167: bool, cov: pd.DataFrame | None, days: int = 7, bounds=None):
    """Render the real Spend tab (AppTest) with an OK-but-empty prefetched CoCo read and return the CoCo
    companion tile plus the read keys (hero_metric patched to record its companions)."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.core.result import QueryResult
    from app.ui import components
    from app.ui.pages.cost_parts import spend

    components._MART_FAIL_BACKOFF.clear()
    empty = QueryResult(df=pd.DataFrame(), ok=True, source="stub")
    keys: list[str] = []
    tiles: list[list[dict]] = []

    def _run(_sql, *_a, **kwargs):
        key = str(kwargs.get("key", ""))
        keys.append(key)
        if key == "coco_ai_reach":
            return QueryResult(df=cov.copy(), ok=True, source="cov stub") if cov is not None else QueryResult(
                df=pd.DataFrame(), ok=False, error="boom")
        return empty

    monkeypatch.setattr("app.core.query.run", _run)
    monkeypatch.setattr(spend, "run", _run)
    monkeypatch.setattr(spend, "run_batch", lambda specs, **_k: {s["key"]: empty for s in specs})
    monkeypatch.setattr(spend, "load_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(spend, "can_open", lambda _page: True)
    monkeypatch.setattr(spend, "request_navigation", lambda *_a, **_k: None)
    monkeypatch.setattr(spend, "has_migration", lambda n, _p: v167 and n <= 167)
    monkeypatch.setattr(spend, "hero_metric", lambda _hero, comps=None: tiles.append(list(comps or [])))
    metering = QueryResult(df=pd.DataFrame({
        "DAY": [account_today() - timedelta(days=1)], "SERVICE_TYPE": ["WAREHOUSE_METERING"],
        "CREDITS_USED": [100.0], "CREDITS_BILLED": [100.0], "CREDITS_ADJUSTMENT": [0.0]}), ok=True, source="m")
    monkeypatch.setattr(spend, "_COCO_TEST_ARGS", {
        "days": days, "bounds": bounds,
        "pre": {"metering_res": metering, "csr_res": empty, "coco_res": empty, "allin_res": empty,
                "napp_res": empty, "csfam_res": empty}}, raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _a = _spend._COCO_TEST_ARGS
        _spend._spend_tab("ALL", _a["days"], 3.0, 3.0, bounds=_a["bounds"], **_a["pre"])

    try:
        at = AppTest.from_function(_app, default_timeout=60)
        at.run()
    finally:
        components._MART_FAIL_BACKOFF.clear()
    assert not at.exception, at.exception
    coco = [c for row in tiles for c in row if str(c.get("label", "")).startswith("— of which CoCo")]
    assert len(coco) == 1, tiles
    return coco[0], keys


def test_r1_016_coco_tile_stale_load_renders_a_dash_not_a_measured_zero(monkeypatch):
    """review: deep reach, a stale last load (the ai_code arm failing for a week), an OK-but-empty read: '—' and
    the help names the last load day -- never a '$0.00' the loader did not measure."""
    from app.logic.formulas import format_usd
    today = account_today()
    stale = today - timedelta(days=9)
    tile, keys = _render_coco_tile(monkeypatch, v167=True, cov=_cov_frame(
        CODE_REACH=today - timedelta(days=364), ALL_REACH=today - timedelta(days=270),
        COVERAGE_FROM=today - timedelta(days=364), LOADED_ON=stale))
    assert tile["value"] == "—" and "coco_ai_reach" in keys
    assert f"Its last full daily load ran {stale:%b} {stale.day}, {stale.year}." in tile["help"]
    # the same reach, loaded today: a measured zero
    tile, _keys = _render_coco_tile(monkeypatch, v167=True, cov=_cov_frame(
        CODE_REACH=today - timedelta(days=364), COVERAGE_FROM=today - timedelta(days=364), LOADED_ON=today))
    assert tile["value"] == format_usd(0.0)


def test_r1_016_coco_tile_names_the_gates_reach_not_the_stamp(monkeypatch):
    """review: before OWNER_REPAIRS step 1 the stamp is the first post-apply run's today-2 while the fact holds
    Cortex Code rows back to first use; a 180d window still fails the gate, and the help must name the gate's
    reach (the earlier of the two), never '3 days'."""
    today = account_today()
    reach = today - timedelta(days=108)
    tile, _keys = _render_coco_tile(monkeypatch, v167=True, days=180, cov=_cov_frame(
        CODE_REACH=reach, ALL_REACH=reach, COVERAGE_FROM=today - timedelta(days=2), LOADED_ON=today))
    assert tile["value"] == "—"
    assert f"The AI fact holds 109 days, from {reach:%b} {reach.day}, {reach.year}." in tile["help"]
    assert "holds 3 days" not in tile["help"] and "last full daily load" not in tile["help"]


def test_r1_016_coco_tile_before_v167_reads_nothing_new(monkeypatch):
    tile, keys = _render_coco_tile(monkeypatch, v167=False, cov=None)
    assert tile["value"] == "—" and "coco_ai_reach" not in keys
    assert "The AI fact" not in tile["help"]
    # holistic #17 (law 12): before V167 the help claims no reach / freshness reason for the '—'
    assert "reach back" not in tile["help"] and "loaded through its end" not in tile["help"]
    assert "the app cannot tell which until V167 is applied" in tile["help"]
    assert "NON-ADDITIVE subset" in tile["help"]                  # the rest of the help is unchanged
    # a failed coverage read after the apply claims nothing either
    tile, keys = _render_coco_tile(monkeypatch, v167=True, cov=None)
    assert tile["value"] == "—" and "coco_ai_reach" in keys and "The AI fact" not in tile["help"]
    assert "does not reach back to this window's start" in tile["help"]
    assert "cannot tell which" not in tile["help"]


def test_coverage_helpers_parse_and_phrase():
    df = pd.DataFrame({"SOURCE_NAME": ["fact_ai_usage_daily", "MART_PATTERN_COST_DAILY", "X"],
                       "COVERAGE_FROM": ["2025-10-02", None, "junk"]})
    assert cost_coverage.coverage_stamps(df) == {"FACT_AI_USAGE_DAILY": date(2025, 10, 2)}
    assert cost_coverage.coverage_stamps(None) == {} and cost_coverage.coverage_stamps(pd.DataFrame()) == {}
    assert cost_coverage.coverage_reach_phrase(date(2026, 9, 30), date(2026, 10, 1)) == \
        "holds 2 days, from Sep 30, 2026"
    assert cost_coverage.coverage_reach_phrase(None, _TODAY) == ""
    assert cost_coverage.coverage_reach_phrase(_TODAY + timedelta(days=1), _TODAY) == ""


def test_ai_fact_coverage_helpers_parse_judge_and_phrase():
    row = cost_coverage.ai_fact_coverage_row(pd.DataFrame({
        "CODE_REACH": ["2026-06-15"], "ALL_REACH": [pd.Timestamp("2026-01-05")], "COVERAGE_FROM": [None],
        "LOADED_ON": ["junk"]}))
    assert row == {"CODE_REACH": date(2026, 6, 15), "ALL_REACH": date(2026, 1, 5)}
    assert cost_coverage.ai_fact_coverage_row(None) == {} and cost_coverage.ai_fact_coverage_row(pd.DataFrame()) == {}
    fresh = cost_coverage.ai_fact_fresh
    assert fresh(_TODAY, _TODAY, _TODAY) and fresh(_TODAY - timedelta(days=1), _TODAY, _TODAY)
    assert not fresh(_TODAY - timedelta(days=2), _TODAY, _TODAY) and not fresh(None, _TODAY, _TODAY)
    assert fresh(date(2026, 8, 30), date(2026, 8, 31), _TODAY)          # a closed window: its own last day
    note = cost_coverage.ai_fact_note
    assert note(date(2026, 9, 30), _TODAY) == "The AI fact holds 2 days, from Sep 30, 2026."
    assert note(None, _TODAY) == ""
    assert note(date(2026, 9, 30), _TODAY, loaded_on=date(2026, 9, 20), window_last=_TODAY) == (
        "The AI fact holds 2 days, from Sep 30, 2026. Its last full daily load ran Sep 20, 2026.")
    assert note(None, _TODAY, loaded_on=_TODAY, window_last=_TODAY) == ""      # fresh: nothing to add


# ------------------------------------------------------------------ Unit costs: the pattern panel render ----

class _Rendered(Exception):
    pass


_PATTERNS = pd.DataFrame({"SAMPLE_TEXT": ["select 1"], "RUNS": [400], "CREDITS": [2.0],
                          "CREDITS_PER_RUN": [0.005], "USERS": [3]})
_REAL_PATTERN_COST = mart27_sql.pattern_cost        # captured before any render patches the module attribute
_V120_CAVEAT = "Rows before Jun 4, 2026 predate the V120 run-count fix and can overstate runs"


def _render_uc(monkeypatch, days, *, v167: bool, pattern_stamp: date | None = None, ai_cov: dict | None = None,
               bounds=None, ai_live: bool = False, ai_mart: bool = False):
    """Render _unit_costs_tab through the Repeated patterns panel (the trend expander's md_dollars label stops
    it), the measured reads failed, has_migration answering for V167 as asked."""
    from app.ui.pages.cost_parts import unit_costs as uc
    lm = "_lm" if bounds is not None else ""
    cov = pd.DataFrame({"SOURCE_NAME": ["MART_PATTERN_COST_DAILY"], "COVERAGE_FROM": [pattern_stamp]})

    def _stop(*_a, **_k):
        raise _Rendered

    live_ai = _ok(pd.DataFrame({"FUNCTION_NAME": ["COMPLETE"], "MODEL_NAME": ["m"], "CREDITS": [1.0]}))
    mart_ai = _ok(pd.DataFrame({"FUNCTION_NAME": ["Snowsight"], "MODEL_NAME": ["n/a"], "CREDITS": [2.0]}))
    fake, seen = _patch(monkeypatch, uc, {f"unit_ai_mart_{days}{lm}": mart_ai if ai_mart else _failed("other"),
                                          f"patterns_ALL_{days}{lm}": _ok(_PATTERNS.copy()),
                                          "unit_pattern_coverage": _ok(cov),
                                          "unit_ai_reach": _ok(_cov_frame(**ai_cov)) if ai_cov else
                                          _failed("other")},
                        run_batch=lambda _jobs, **_k: {"q": _failed("other"), "p": _failed("other"),
                                                       "ai": live_ai if ai_live else _failed("other")},
                        panel_help=lambda *_a, **_k: None, md_dollars=_stop)
    monkeypatch.setattr(uc, "has_migration", lambda n, _p: v167 and n <= 167)
    sqls: list[str] = []
    monkeypatch.setattr(uc.mart27_sql, "pattern_cost",
                        lambda *a, **k: sqls.append(_REAL_PATTERN_COST(*a, **k)) or sqls[-1])
    fake._off = (f"uc_full_window_ALL_{days}",)
    fake.column_config = SimpleNamespace(NumberColumn=lambda *_a, **_k: None)
    with pytest.raises(_Rendered):
        uc._unit_costs_tab({"company": "ALL", "days": days, "database": "", "schema_contains": "",
                            "bounds": bounds, "warehouse_contains": "", "user_contains": ""}, 3.0, 2.2)
    return fake, seen, sqls


def test_pattern_restamp_pre_v167_path_is_unchanged_and_reads_no_stamp(monkeypatch):
    fake, seen, sqls = _render_uc(monkeypatch, 365, v167=False)
    caps = fake.text("caption")
    assert "unit_pattern_coverage" not in seen["runs"] and "unit_ai_reach" not in seen["runs"]
    assert "Measured QUERY_ATTRIBUTION_HISTORY compute (90d), grouped by" in caps
    assert "A trailing window reads at most the last 90 days" in caps
    assert "the repeated-pattern panel reads at most the last 90 days from its mart" in caps
    assert sqls == [_REAL_PATTERN_COST(365, "ALL", 25)]


def test_pattern_restamp_a_covered_window_reads_it_whole_and_drops_the_disclosure(monkeypatch):
    stamp = account_today() - timedelta(days=400)
    fake, seen, sqls = _render_uc(monkeypatch, 365, v167=True, pattern_stamp=stamp)
    caps = fake.text("caption")
    assert seen["runs"].count("unit_pattern_coverage") == 1
    assert "Measured QUERY_ATTRIBUTION_HISTORY compute (365d), grouped by" in caps
    assert "reads at most the last" not in caps and _V120_CAVEAT not in caps
    assert "The AI, task-graph pipeline and repeated-pattern panels below follow the page window" in caps
    assert "WHERE p.DAY >= DATEADD('day', -365, CURRENT_DATE())" in sqls[0]


def test_pattern_restamp_an_uncovered_window_names_the_stamped_reach(monkeypatch):
    stamp = account_today() - timedelta(days=200)
    fake, _seen, sqls = _render_uc(monkeypatch, 365, v167=True, pattern_stamp=stamp)
    caps = fake.text("caption")
    assert "compute (200d), grouped by" in caps and "A trailing window reads at most the last 200 days" in caps
    assert "the repeated-pattern panel reads at most the last 200 days from its mart" in caps
    assert "WHERE p.DAY >= DATEADD('day', -200, CURRENT_DATE())" in sqls[0]
    # V167 applied but no stamp yet (no pattern run since the apply): the pre-V167 cap
    fake, _seen, sqls = _render_uc(monkeypatch, 365, v167=True, pattern_stamp=None)
    assert "compute (90d), grouped by" in fake.text("caption")
    assert sqls == [_REAL_PATTERN_COST(365, "ALL", 25)]


@pytest.mark.parametrize(("stamp", "caveat"), [(date(2025, 10, 2), False), (date(2026, 9, 28), True), (None, True)])
def test_pattern_restamp_current_year_caveat_follows_the_clean_horizon(monkeypatch, stamp, caveat):
    bounds = (date(2026, 1, 1), date(2026, 10, 2))
    fake, _seen, _sqls = _render_uc(monkeypatch, 273, v167=True, pattern_stamp=stamp, bounds=bounds)
    caps = fake.text("caption")
    assert (_V120_CAVEAT in caps) is caveat, caps


def test_r1_016_unit_costs_ai_help_names_the_all_source_gates_reach(monkeypatch):
    """review: the Functions-only help names the reach ai_costs_by_model's gate tested (ALL_REACH: the earlier
    of the all-source first day and the horizon-floored stamp), never the raw stamp -- before OWNER_REPAIRS
    step 1 that stamp is today-2 while the fact holds months of rows."""
    today = account_today()
    reach = today - timedelta(days=30)
    cov = {"CODE_REACH": today - timedelta(days=10), "ALL_REACH": reach, "COVERAGE_FROM": today - timedelta(days=2),
           "LOADED_ON": today}
    _fake, seen, _sqls = _render_uc(monkeypatch, 365, v167=True, ai_cov=cov, ai_live=True)
    ai = next(k for row in seen["kpis"] for k in row if k["label"].startswith("AI spend"))
    assert ai["label"].endswith("· Functions only")
    assert "does not cover this whole window (or could not be read)" in ai["help"]
    assert f"The AI fact holds 31 days, from {reach:%b} {reach.day}, {reach.year}." in ai["help"]
    assert "holds 3 days" not in ai["help"] and "holds 11 days" not in ai["help"]
    assert seen["runs"].count("unit_ai_reach") == 1
    # a failed coverage read names nothing
    _fake, seen, _sqls = _render_uc(monkeypatch, 365, v167=True, ai_cov=None, ai_live=True)
    ai = next(k for row in seen["kpis"] for k in row if k["label"].startswith("AI spend"))
    assert "The AI fact" not in ai["help"]
    # before the apply: no V167 read, no reach
    _fake, seen, _sqls = _render_uc(monkeypatch, 365, v167=False, ai_live=True)
    ai = next(k for row in seen["kpis"] for k in row if k["label"].startswith("AI spend"))
    assert "The AI fact holds" not in ai["help"] and "unit_ai_reach" not in seen["runs"]
    # the mart answered: the full-window KPI needs no reach, so the coverage is never read
    _fake, seen, _sqls = _render_uc(monkeypatch, 365, v167=True, ai_cov=cov, ai_mart=True)
    ai = next(k for row in seen["kpis"] for k in row if k["label"].startswith("AI spend"))
    assert not ai["label"].endswith("· Functions only") and "unit_ai_reach" not in seen["runs"]


# ----------------------------------------------------------------------------- the other captions ----

def test_r2_014_task_graph_caption_claims_root_day_keying_only_after_v167():
    """review: V167 stops NEW phantom rows; the nightly sweep clears D-3 onward, but every older day keeps its
    phantoms until OWNER_REPAIRS step 4 (the 364-day rebuild) -- so the caption claims root-day keying only for
    the days loaded since V167 and says older days wait for the rebuild (law 12: claim what V167 guarantees)."""
    src = read("app/ui/pages/cost_parts/unit_costs.py")
    assert ('"Pipeline label = the graph\'s root task"\n'
            '                             + ("; on days loaded since V167 a run counts on the day its root task "\n'
            '                                "started (older days can still show a child-named row until the "\n'
            '                                "owner\'s task-graph rebuild)." if has_migration(167, _PAGE) else "."))'
            ) in src
    assert "on the mart a run counts on the day its root task started (V167)" not in src


@pytest.mark.parametrize("v166", [False, True])
def test_c10_unknown_application_note_states_each_legs_session_rule(monkeypatch, v166):
    """holistic #18: the mart (FACT_APP_COST_DAILY) resolves a query's session relative to the QUERY'S OWN DAY --
    each day's last reload runs with lo = that day and keeps SESSIONS with CREATED_ON >= DATEADD('day', -30, :lo)
    (V166; -7 in V077) -- while only the live fallback pads SESSION_PAD_DAYS from the page WINDOW's start. Law 12:
    before V166 is applied every mart day used 7, so the mart leg says 7 and names no V166 rule."""
    from app.data import app_cost_sql
    from app.ui.pages.cost_parts import spend
    monkeypatch.setattr(spend, "has_migration", lambda n, _p: v166 and n <= 166)
    pad = app_cost_sql.SESSION_PAD_DAYS
    mart = (f"{pad} days before the query's day (7 on days the daily loader wrote before V166)" if v166
            else "7 days before the query's day")
    note = spend._unknown_app_note()
    assert note == (
        "'(unknown)' = a query whose session reported no application, or whose session could not be found: on "
        f"the FACT_APP_COST_DAILY mart, one opened more than {mart}; on the live fallback, one opened more than "
        f"{pad} days before the window began; or a system/task session with no SESSIONS row.")
    assert ("V166" in note) is v166 and note.count("before the window began") == 1


@pytest.mark.parametrize("v166", [False, True])
def test_c10_unknown_application_caption_renders_the_gated_note(monkeypatch, v166):
    """The rendered Cost-by-application caption carries the gated note, never the old window-start-only rule."""
    from app.ui.pages.cost_parts import spend
    from tests.test_cost_spend_honesty import _app_frame, _attribution, _captions
    from tests.test_cost_spend_honesty import _ok as _qok
    monkeypatch.setattr(spend, "has_migration", lambda n, _p: v166 and n <= 166)
    at, _charts = _attribution(monkeypatch, daily=_qok(pd.DataFrame()), toggles=("spend_app_cost_load",),
                               app=_qok(_app_frame()))
    caps = _captions(at)
    assert spend._unknown_app_note() in caps
    assert "days before the window began, or 7 on days" not in caps
    src = read("app/ui/pages/cost_parts/spend.py")
    assert "more than 30 days earlier" not in src
    assert "or could not be joined to a session" not in src


class _MapperSt:
    def __init__(self):
        self.captions: list[str] = []

    def expander(self, *_a, **_k):
        from contextlib import nullcontext
        return nullcontext()

    def columns(self, spec):
        from contextlib import nullcontext
        return [nullcontext() for _ in range(spec if isinstance(spec, int) else len(spec))]

    def selectbox(self, _label, options, **_k):
        return options[0]

    def code(self, *_a, **_k):
        pass

    def caption(self, text, *_a, **_k):
        self.captions.append(str(text))

    def button(self, *_a, **_k):
        return False


@pytest.mark.parametrize("v167", [False, True])
def test_r2_010_mapper_caption_tells_the_pattern_truth_for_each_side_of_the_apply(monkeypatch, v167):
    from app.ui.pages import cost
    fake = _MapperSt()
    monkeypatch.setattr(cost, "st", fake)
    monkeypatch.setattr(cost, "has_migration", lambda n, _p: v167 and n <= 167)
    cost._unmapped_mapper(pd.DataFrame({"ENTITY": ["WH_X"], "GRAIN": ["WAREHOUSE"]}), is_operator=False)
    text = "\n".join(fake.captions)
    assert "Pattern costs are not part of the reconcile" in text
    if v167:
        assert "their own daily loader re-stamps its trailing 3 days" in text
        assert "CALL SP_LOAD_PATTERN_COST(N) (N up to 364)" in text and "do not re-run" not in text
    else:
        assert "do not re-run SP_LOAD_PATTERN_COST before V167 is applied" in text
        assert "adds a second row under the new company" in text


@pytest.mark.parametrize(("v167", "stamp", "first_day", "restamped"), [
    (False, None, "2026-09-01", False),                    # pre-apply: the caveat always stays
    (True, None, "2026-09-01", True),                      # after V120's horizon: clean once V167 ran
    (True, None, "2026-05-01", False),                     # before it, with no deeper stamp
    (True, date(2025, 10, 2), "2026-05-01", True),         # the owner's CALL(364) reached it
])
def test_r2_010_compare_v120_clause_follows_the_clean_horizon(monkeypatch, v167, stamp, first_day, restamped):
    from app.ui.pages.cost_parts import compare
    monkeypatch.setattr(compare, "has_migration", lambda n, _p: v167 and n <= 167)
    reads = []
    monkeypatch.setattr(compare, "run", lambda sql, **k: reads.append(k["key"]) or _ok(
        pd.DataFrame({"SOURCE_NAME": ["MART_PATTERN_COST_DAILY"], "COVERAGE_FROM": [stamp]})))
    assert compare._pattern_window_restamped(first_day) is restamped
    assert reads == (["cmp_pattern_coverage"] if v167 else [])
    src = read("app/ui/pages/cost_parts/compare.py")
    assert '+ ("" if _pattern_window_restamped(min(a0, b0)) else' in src


# ---------------------------------------------------------------------------------------------- R2-052 ----

def test_r2_052_live_cortex_code_day_keys_are_central():
    for sql, key in ((cortex_sql.cortex_code_user_daily("ALL"), "USAGE_DATE"),
                     (cortex_sql.cortex_code_daily(30), "DAY"),
                     (cortex_sql.cortex_code_token_types(), "USAGE_DATE")):
        assert f"CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE AS {key}" in sql, key
        assert "C.USAGE_TIME::DATE" not in sql
