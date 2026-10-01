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
    assert _STAMP_LOCK in stamped and "COVERAGE_FROM) AS CF" in stamped
    assert _STAMP_SRC in stamped and "SOURCE_NAME = 'FACT_AI_USAGE_DAILY'" in stamped
    import sqlglot
    sqlglot.parse_one(stamped, read="snowflake")


def test_r1_016_ai_costs_by_model_stamp_gates_all_sources():
    sql = mart27_sql.ai_costs_by_model(365, stamped=True)
    assert "SELECT MIN(a2.DAY) AS FIRST_DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY a2" in sql
    assert "SOURCE <> 'Functions'" not in sql                   # Code + Functions, as before
    assert "<= DATEADD('day', -365 + 1, CURRENT_DATE())" in sql


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
    assert canary.MIGRATION_GATED == {"mart27.fact_coverage_from": 167}
    assert "mart27.fact_coverage_from" not in canary.EXPECTED_GAPS
    assert canary.gated_out("mart27.fact_coverage_from", frozenset(range(1, 167)))
    assert not canary.gated_out("mart27.fact_coverage_from", frozenset(range(1, 168)))
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


def _lite(sql: str) -> str:
    out = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    out = out.replace("CURRENT_DATE()", f"'{_TODAY.isoformat()}'").replace("ANY_VALUE(", "MAX(")
    out = re.sub(r"--[^\n]*", "", out)
    for tok in ("::", "QUALIFY", "IFF(", "CONVERT_TIMEZONE"):
        assert tok not in out, tok
    return out


def _ai_db(stamp: str | None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("LEAST", -1, _least)
    con.execute("CREATE TABLE FACT_AI_USAGE_DAILY (DAY TEXT, USER_NAME TEXT, SOURCE TEXT, MODEL_NAME TEXT, "
                "EMAIL TEXT, FIRST_TS TEXT, LAST_TS TEXT, REQUESTS INT, TOKENS INT, CREDITS REAL)")
    rows = [("2026-06-15", "U1", "Snowsight", "n/a", 5.0),        # Code first use mid-year
            ("2026-09-20", "U2", "CLI", "n/a", 7.0),
            ("2026-01-10", "ACCOUNT", "Functions", "llama", 3.0)]  # Functions started earlier
    con.executemany("INSERT INTO FACT_AI_USAGE_DAILY VALUES (?, ?, ?, ?, NULL, NULL, NULL, 1, 10, ?)", rows)
    con.execute("CREATE TABLE SOURCE_FRESHNESS_STATE (SOURCE_NAME TEXT, COVERAGE_FROM TEXT)")
    con.execute("INSERT INTO SOURCE_FRESHNESS_STATE VALUES ('FACT_AI_USAGE_DAILY', ?)", (stamp,))
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
    model = _credits(con, mart27_sql.ai_costs_by_model(365, stamped=True), "CREDITS")
    assert model == (15.0 if answered else 0.0)                  # Code + Functions
    assert _credits(con, mart27_sql.ai_costs_by_model(365), "CREDITS") == 0.0
    rollup = _credits(con, mart27_sql.ai_code_user_rollup(365, "ALL", stamped=True), "TOTAL_CREDITS")
    assert rollup == (12.0 if answered else 0.0)


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
    spend = read("app/ui/pages/cost_parts/spend.py")
    assert "'—' until the fact loads." not in spend
    assert ("'—' while FACT_AI_USAGE_DAILY does not reach back to this window's start (or could "
            "\"\n                 \"not be read).") in spend
    uc = read("app/ui/pages/cost_parts/unit_costs.py")
    assert "was unavailable on this refresh" not in uc
    assert "does not cover this whole window (or could not be read)" in uc


def test_r1_016_spend_reach_is_read_only_after_v167_and_named(monkeypatch):
    from app.ui.pages.cost_parts import spend
    monkeypatch.setattr(spend, "has_migration", lambda n, _p: False)
    monkeypatch.setattr(spend, "run", lambda *_a, **_k: pytest.fail("no V167 column read before the apply"))
    assert spend._ai_fact_reach() == ""
    stamp = account_today() - timedelta(days=364)
    seen = []
    monkeypatch.setattr(spend, "has_migration", lambda n, _p: n <= 167)
    monkeypatch.setattr(spend, "run", lambda sql, **k: seen.append((sql, k["key"])) or _ok(
        pd.DataFrame({"SOURCE_NAME": ["FACT_AI_USAGE_DAILY"], "COVERAGE_FROM": [stamp]})))
    assert spend._ai_fact_reach() == f"holds 365 days, from {stamp:%b} {stamp.day}, {stamp.year}"
    assert seen == [(mart27_sql.fact_coverage_from("FACT_AI_USAGE_DAILY"), "coco_ai_reach")]
    monkeypatch.setattr(spend, "run", lambda *_a, **_k: _failed("missing_column"))
    assert spend._ai_fact_reach() == "" and spend._ai_fact_stamp() is None   # a failed read never guesses
    # the stamp is read only when the tile has no figure, never on a populated one; an OK-but-empty stamped
    # read whose stamp reaches the gate's own start day is a VERIFIED zero ($0.00), anything else names the reach
    body = read("app/ui/pages/cost_parts/spend.py")
    block = body[body.index("    _coco_reach = \"\"\n    if coco_usd is None:"):body.index("    # rec #8: the all-in")]
    assert "_coco_stamp = _ai_fact_stamp()" in block
    assert ("_coco_start = bounds[0] if bounds is not None else account_today() - timedelta(days=int(days) - 1)"
            in block)
    assert "if _coco_verified_zero(coco_res, _coco_stamp, _coco_start):\n            coco_usd = 0.0" in block
    assert "_coco_reach = coverage_reach_phrase(_coco_stamp, account_today())" in block
    start = date(2026, 9, 2)
    empty, rows = _ok(pd.DataFrame()), _ok(pd.DataFrame({"TOTAL_CREDITS": [1.0]}))
    assert spend._coco_verified_zero(empty, date(2025, 10, 2), start)          # covered and empty: $0.00
    assert not spend._coco_verified_zero(empty, date(2026, 9, 3), start)       # stamp short of the start
    assert not spend._coco_verified_zero(empty, None, start)                   # no stamp: no claim
    assert not spend._coco_verified_zero(_failed("timeout"), date(2025, 10, 2), start)
    assert not spend._coco_verified_zero(rows, date(2025, 10, 2), start)       # rows: the figure stands
    assert not spend._coco_verified_zero(None, date(2025, 10, 2), start)


def test_coverage_helpers_parse_and_phrase():
    df = pd.DataFrame({"SOURCE_NAME": ["fact_ai_usage_daily", "MART_PATTERN_COST_DAILY", "X"],
                       "COVERAGE_FROM": ["2025-10-02", None, "junk"]})
    assert cost_coverage.coverage_stamps(df) == {"FACT_AI_USAGE_DAILY": date(2025, 10, 2)}
    assert cost_coverage.coverage_stamps(None) == {} and cost_coverage.coverage_stamps(pd.DataFrame()) == {}
    assert cost_coverage.coverage_reach_phrase(date(2026, 9, 30), date(2026, 10, 1)) == \
        "holds 2 days, from Sep 30, 2026"
    assert cost_coverage.coverage_reach_phrase(None, _TODAY) == ""
    assert cost_coverage.coverage_reach_phrase(_TODAY + timedelta(days=1), _TODAY) == ""


# ------------------------------------------------------------------ Unit costs: the pattern panel render ----

class _Rendered(Exception):
    pass


_PATTERNS = pd.DataFrame({"SAMPLE_TEXT": ["select 1"], "RUNS": [400], "CREDITS": [2.0],
                          "CREDITS_PER_RUN": [0.005], "USERS": [3]})
_REAL_PATTERN_COST = mart27_sql.pattern_cost        # captured before any render patches the module attribute
_V120_CAVEAT = "Rows before Jun 4, 2026 predate the V120 run-count fix and can overstate runs"


def _render_uc(monkeypatch, days, *, v167: bool, pattern_stamp: date | None = None, ai_stamp: date | None = None,
               bounds=None, ai_live: bool = False):
    """Render _unit_costs_tab through the Repeated patterns panel (the trend expander's md_dollars label stops
    it), the measured reads failed, has_migration answering for V167 as asked."""
    from app.ui.pages.cost_parts import unit_costs as uc
    lm = "_lm" if bounds is not None else ""
    cov = pd.DataFrame({"SOURCE_NAME": ["FACT_AI_USAGE_DAILY", "MART_PATTERN_COST_DAILY"],
                        "COVERAGE_FROM": [ai_stamp, pattern_stamp]})

    def _stop(*_a, **_k):
        raise _Rendered

    live_ai = _ok(pd.DataFrame({"FUNCTION_NAME": ["COMPLETE"], "MODEL_NAME": ["m"], "CREDITS": [1.0]}))
    fake, seen = _patch(monkeypatch, uc, {f"unit_ai_mart_{days}{lm}": _failed("other"),
                                          f"patterns_ALL_{days}{lm}": _ok(_PATTERNS.copy()),
                                          "unit_fact_coverage": _ok(cov)},
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
    assert "unit_fact_coverage" not in seen["runs"]
    assert "Measured QUERY_ATTRIBUTION_HISTORY compute (90d), grouped by" in caps
    assert "A trailing window reads at most the last 90 days" in caps
    assert "the repeated-pattern panel reads at most the last 90 days from its mart" in caps
    assert sqls == [_REAL_PATTERN_COST(365, "ALL", 25)]


def test_pattern_restamp_a_covered_window_reads_it_whole_and_drops_the_disclosure(monkeypatch):
    stamp = account_today() - timedelta(days=400)
    fake, seen, sqls = _render_uc(monkeypatch, 365, v167=True, pattern_stamp=stamp)
    caps = fake.text("caption")
    assert seen["runs"].count("unit_fact_coverage") == 1
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


def test_r1_016_unit_costs_ai_help_names_the_stamped_reach(monkeypatch):
    stamp = account_today() - timedelta(days=30)
    _fake, seen, _sqls = _render_uc(monkeypatch, 365, v167=True, ai_stamp=stamp, ai_live=True)
    ai = next(k for row in seen["kpis"] for k in row if k["label"].startswith("AI spend"))
    assert ai["label"].endswith("· Functions only")
    assert "does not cover this whole window (or could not be read)" in ai["help"]
    assert f"The AI fact holds 31 days, from {stamp:%b} {stamp.day}, {stamp.year}." in ai["help"]
    _fake, seen, _sqls = _render_uc(monkeypatch, 365, v167=False, ai_live=True)
    ai = next(k for row in seen["kpis"] for k in row if k["label"].startswith("AI spend"))
    assert "The AI fact holds" not in ai["help"]


# ----------------------------------------------------------------------------- the other captions ----

def test_r2_014_task_graph_caption_claims_root_day_keying_only_after_v167():
    src = read("app/ui/pages/cost_parts/unit_costs.py")
    assert ('"Pipeline label = the graph\'s root task"\n'
            '                             + ("; on the mart a run counts on the day its root task started (V167)."\n'
            '                                if has_migration(167, _PAGE) else "."))') in src


def test_c10_unknown_application_caption_names_the_30_day_session_pad():
    src = read("app/ui/pages/cost_parts/spend.py")
    assert ("session that reported no application, or whose session could not be found (opened "
            "\"\n                    \"more than 30 days earlier, or a system/task session with no SESSIONS "
            "row)") in src
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
