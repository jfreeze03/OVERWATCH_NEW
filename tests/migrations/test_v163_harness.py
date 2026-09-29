"""Executed harness for V163's new SP_ALERT_SCAN_DAILY arms -- [28] COST_AI_USER_RUNAWAY, [29]
SEC_TRUST_REGRESSION -- and the reworded [07] SEC_FAILED_LOGINS.

Each arm's OWN INSERT (read from the V163 migration, never a hand-written twin) runs in an in-memory sqlite. The
translation is minimal and fails closed: FQNs dropped, the Central clock pinned (TODAY_D), the one :bind
(:ai_credit_price) made a literal, ``ROUND(..)::INT`` cast, ``LISTAGG(DISTINCT x, '+') WITHIN GROUP (ORDER BY x)``
and MEDIAN as aggregates, IFF / GREATEST / LEAST / LEFT / DATEADD / TO_VARCHAR / TRY_TO_DOUBLE / TRY_TO_BOOLEAN /
COMPANY_FOR_USER shimmed with Snowflake semantics, and the ``b (cols)`` derived table turned into a CTE. ALERT_EVENTS
carries its V004 widths as CHECKs, so an over-long TITLE, DETAIL, COMPANY or key fails here as it would in
Snowflake. A DAY is 'YYYY-MM-DD'; TODAY is the Central date the daily scan sees.

``_Db.runaway`` is the entry point the #37b parity lock (tests/test_ai_runaway_parity.py) drives: the rows the arm
[28] text raises for a FACT_AI_USAGE_DAILY fixture.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import date, timedelta

import pytest

from tests._source import read

_MIG = read("snowflake/migrations/V163__ai_runaway_trust_regression.sql")
_V075 = read("snowflake/migrations/V075__security_operating_model.sql")
_TODAY = date(2026, 9, 29)                   # a Tuesday; the scan runs ~07:00 Central
_AI_PRICE = 2.2
_AI, _TRUST = "COST_AI_USER_RUNAWAY", "SEC_TRUST_REGRESSION"
_EVENT_COLS = ("RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE", "DEDUPE_KEY")


def _day(k: int, today: date = _TODAY) -> str:
    return (today + timedelta(days=k)).isoformat()


# ============================================================================================================
# The arms, from the migration
# ============================================================================================================
def _proc_body(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}(")
    o = text.index("$$", s) + 2
    return text[o:text.index("$$;", o)]


_BODY = _proc_body(_MIG, "SP_ALERT_SCAN_DAILY")


def _block(start: str, end: str) -> str:
    i = _BODY.index(start)
    return _BODY[i:_BODY.index(end, i)]


def _insert(block: str) -> str:
    """The arm's INSERT statement (its EXCEPTION handler dropped)."""
    return block[block.index("        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):block.index(";\n    EXCEPTION")]


_ARM28 = _insert(_block("    -- [28] COST_AI_USER_RUNAWAY", "    -- [29] SEC_TRUST_REGRESSION"))
_ARM29 = _insert(_block("    -- [29] SEC_TRUST_REGRESSION", "    -- [17] PIPE_REF_GAP"))
_ARM07 = _insert(_block("    -- [07] SEC_FAILED_LOGINS", "    -- [08] COST_BUDGET_PACE"))


# ============================================================================================================
# Snowflake -> sqlite
# ============================================================================================================
def _lex(sql: str) -> list[tuple[str, str]]:
    """-> [(kind, text)]: 'code', 'str' (the literal WITH its quotes) or 'comment'. No backslash escapes (the
    arms carry none: asserted)."""
    assert "\\" not in sql
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i) or sql[i] == "'":
            if buf:
                out.append(("code", "".join(buf)))
                buf = []
            if sql[i] == "'":
                j = i + 1
                while True:
                    j = sql.index("'", j)
                    if sql[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    break
                out.append(("str", sql[i:j + 1]))
                i = j + 1
            else:
                j = sql.find("\n", i)
                j = n if j < 0 else j
                out.append(("comment", sql[i:j]))
                i = j
        else:
            buf.append(sql[i])
            i += 1
    if buf:
        out.append(("code", "".join(buf)))
    return out


def _derived_to_cte(sql: str) -> str:
    """``SELECT b.* FROM (<inner>) b (cols) WHERE ...`` -> CTE ``b(cols)`` (sqlite has no derived column list)."""
    m = re.search(r"\n(?P<sel>[ ]*SELECT b\.RULE_ID[^\n]*)\n[ ]*FROM \(\n(?P<inner>.*)\n[ ]*\) b "
                  r"\((?P<cols>[^)]*)\)\n", sql, re.S)
    assert m, "the arm lost its SELECT b.* FROM (...) b (cols) shape"
    return (sql[:m.start()].rstrip() + f",\nb({m['cols']}) AS (\n{m['inner']}\n)\n{m['sel']}\nFROM b\n"
            + sql[m.end():])


_LISTAGG_RE = re.compile(r"LISTAGG\(DISTINCT ([\w.]+), '\+'\) WITHIN GROUP \(ORDER BY \1\)")


def _to_sqlite(sql: str) -> str:
    s = sql.replace("DBA_MAINT_DB.OVERWATCH.", "")
    s = s.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE", "TODAY_D()")
    s = s.replace("CURRENT_DATE()", "TODAY_D()")
    out = []
    for kind, text in _lex(s):
        if kind == "comment":
            continue
        if kind == "code":
            text = text.replace(":ai_credit_price", repr(_AI_PRICE))
            assert re.search(r"(?<!:):\w", text) is None, text
        out.append(text)
    s = "".join(out)
    s = re.sub(r"(ROUND\([^()']*\))::INT\b", r"CAST(\1 AS INTEGER)", s)
    s = _LISTAGG_RE.sub(r"LISTAGG_PLUS(DISTINCT \1)", s)
    s = _derived_to_cte(s)
    code = "".join(t for k, t in _lex(s) if k == "code")
    assert "::" not in code and "WITHIN GROUP" not in code and "CONVERT_TIMEZONE" not in code, code
    return s


# ---- shims (Snowflake semantics) -----------------------------------------------------------------------------
def _nullsafe(fn):
    return lambda *a: None if any(v is None for v in a) else fn(*a)


def _dateadd(unit, n, x):
    if unit is None or n is None or x is None:
        return None
    assert str(unit).lower() == "day", unit
    return (date.fromisoformat(str(x)[:10]) + timedelta(days=int(n))).isoformat()


def _to_varchar(x, fmt=None):
    if x is None:
        return None
    if fmt is not None:
        assert fmt == "YYYY-MM-DD HH24:MI", fmt
        return str(x)[:16]
    return x if isinstance(x, str) else str(x)


def _try_to_double(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_TRUE_WORDS = {"true", "t", "yes", "y", "on", "1"}
_FALSE_WORDS = {"false", "f", "no", "n", "off", "0"}


def _try_to_boolean(v):
    word = str(v).strip().lower() if v is not None else None
    if word in _TRUE_WORDS:
        return 1
    if word in _FALSE_WORDS:
        return 0
    return None


class _Median:
    def __init__(self) -> None:
        self.v: list[float] = []

    def step(self, x) -> None:
        if x is not None:
            self.v.append(float(x))

    def finalize(self):
        return statistics.median(self.v) if self.v else None


class _ListAggPlus:
    """LISTAGG(DISTINCT x, '+') WITHIN GROUP (ORDER BY x) -- DISTINCT is applied by sqlite."""

    def __init__(self) -> None:
        self.v: list[str] = []

    def step(self, x) -> None:
        if x is not None:
            self.v.append(str(x))

    def finalize(self):
        return "+".join(sorted(self.v)) if self.v else None


_SCHEMAS = {
    "ALERT_CONFIG": "RULE_ID TEXT, SEVERITY TEXT, ENABLED INT, THRESHOLD_NUM REAL",
    "SETTINGS": "KEY TEXT, VALUE TEXT",
    "FACT_AI_USAGE_DAILY": "DAY TEXT NOT NULL, USER_NAME TEXT NOT NULL CHECK (length(USER_NAME) <= 200), "
                           "SOURCE TEXT NOT NULL, MODEL_NAME TEXT NOT NULL DEFAULT 'n/a', CREDITS REAL",
    "SECURITY_TRUST_SNAPSHOT": "DAY TEXT NOT NULL, SCANNER_ID TEXT NOT NULL CHECK (length(SCANNER_ID) <= 200), "
                               "SCANNER_NAME TEXT CHECK (length(SCANNER_NAME) <= 500), SEVERITY TEXT, "
                               "TOTAL_AT_RISK_COUNT INT, SCANNED_AT TEXT",
    "FACT_LOGIN_DAILY": "DAY TEXT NOT NULL, USER_NAME TEXT NOT NULL CHECK (length(USER_NAME) <= 200), "
                        "COMPANY TEXT NOT NULL, LOGINS INT, FAILED_LOGINS INT",
    # V004 widths as CHECKs
    "ALERT_EVENTS": "EVENT_ID INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "RULE_ID TEXT NOT NULL CHECK (length(RULE_ID) <= 60), "
                    "COMPANY TEXT NOT NULL DEFAULT 'ALL' CHECK (length(COMPANY) <= 40), "
                    "SEVERITY TEXT NOT NULL CHECK (length(SEVERITY) <= 20), "
                    "TITLE TEXT NOT NULL CHECK (length(TITLE) <= 300), "
                    "DETAIL TEXT CHECK (length(DETAIL) <= 2000), METRIC_VALUE REAL, "
                    "DEDUPE_KEY TEXT CHECK (length(DEDUPE_KEY) <= 300), STATUS TEXT NOT NULL DEFAULT 'OPEN'",
}
# V004 seeds + the V163 seeds (RULE_ID, SEVERITY, THRESHOLD_NUM)
_RULES = ((_AI, "HIGH", 2.0), (_TRUST, "HIGH", 1.0), ("SEC_FAILED_LOGINS", "HIGH", 10.0))


class _Db:
    def __init__(self, *, today: date = _TODAY, companies: dict | None = None,
                 settings: dict | None = None, rules: dict | None = None) -> None:
        self.today = today
        self.companies = companies or {}
        con = sqlite3.connect(":memory:", isolation_level=None)
        fns = {
            "TODAY_D": (0, lambda: self.today.isoformat()),
            "DATEADD": (3, _dateadd), "IFF": (3, lambda c, a, b: a if c else b),
            "GREATEST": (-1, _nullsafe(lambda *a: max(a))), "LEAST": (-1, _nullsafe(lambda *a: min(a))),
            "LEFT": (2, _nullsafe(lambda s, n: str(s)[:int(n)])), "TO_VARCHAR": (-1, _to_varchar),
            "TRY_TO_DOUBLE": (1, _try_to_double), "TRY_TO_BOOLEAN": (1, _try_to_boolean),
            # V044: the COMPANY_SCOPE override / role evidence, else 'UNKNOWN' -- never NULL
            "COMPANY_FOR_USER": (1, lambda u: self.companies.get(u, "UNKNOWN")),
        }
        for name, (narg, fn) in fns.items():
            con.create_function(name, narg, fn)
        con.create_aggregate("MEDIAN", 1, _Median)
        con.create_aggregate("LISTAGG_PLUS", 1, _ListAggPlus)
        for table, cols in _SCHEMAS.items():
            con.execute(f"CREATE TABLE {table} ({cols})")
        cfg = {**{r: (sev, thr, 1) for r, sev, thr in _RULES}, **(rules or {})}
        for rid, (sev, thr, enabled) in cfg.items():
            con.execute("INSERT INTO ALERT_CONFIG VALUES (?, ?, ?, ?)", (rid, sev, enabled, thr))
        for k, v in (settings or {}).items():
            con.execute("INSERT INTO SETTINGS VALUES (?, ?)", (k, v))
        self.con = con

    # -- fixtures ----------------------------------------------------------------------------------------------
    def ai(self, rows) -> _Db:
        """rows: (day, user, credits[, source])."""
        for r in rows:
            day, user, cr, src = (*r, "Snowsight")[:4]
            self.con.execute("INSERT INTO FACT_AI_USAGE_DAILY (DAY, USER_NAME, SOURCE, CREDITS) VALUES (?, ?, ?, ?)",
                             (day, user, src, cr))
        return self

    def trust(self, rows) -> _Db:
        """rows: (day, scanner_id, severity, count[, name[, scanned_at]])."""
        for r in rows:
            day, sid, sev, n, name, at = (*r, "Scanner " + r[1], r[0] + " 06:10:00")[:6]
            self.con.execute("INSERT INTO SECURITY_TRUST_SNAPSHOT VALUES (?, ?, ?, ?, ?, ?)",
                             (day, sid, name, sev, n, at))
        return self

    def logins(self, rows) -> _Db:
        """rows: (day, user, logins, failed)."""
        for day, user, logins, failed in rows:
            self.con.execute("INSERT INTO FACT_LOGIN_DAILY VALUES (?, ?, 'ALFA', ?, ?)", (day, user, logins, failed))
        return self

    # -- runs ----------------------------------------------------------------------------------------------------
    def run(self, arm: str) -> list[dict]:
        """Execute the arm's INSERT; -> the rows it inserted this run."""
        before = self.con.execute("SELECT COALESCE(MAX(EVENT_ID), 0) FROM ALERT_EVENTS").fetchone()[0]
        self.con.execute(_to_sqlite(arm))
        cur = self.con.execute(f"SELECT {', '.join(_EVENT_COLS)} FROM ALERT_EVENTS WHERE EVENT_ID > ? "
                               "ORDER BY EVENT_ID", (before,))
        return [dict(zip(_EVENT_COLS, r, strict=True)) for r in cur.fetchall()]

    def runaway(self) -> list[dict]:
        return self.run(_ARM28)

    def regression(self) -> list[dict]:
        return self.run(_ARM29)


def _steady(user: str, values, *, first: int = -60, step: int = 1, source: str = "Snowsight") -> list[tuple]:
    """One row per active day from TODAY+first on, cycling through ``values``."""
    vals = list(values)
    return [(_day(first + i * step), user, vals[i % len(vals)], source) for i in range(len(vals) * 3)
            if first + i * step < -3]


# ============================================================================================================
# [28] COST_AI_USER_RUNAWAY
# ============================================================================================================
def test_runaway_fires_on_both_signals():
    db = _Db(companies={"ALICE": "ALFA"}).ai([*_steady("ALICE", [2.5, 3.0, 3.5, 2.8, 3.2]),
                                              (_day(-1), "ALICE", 44.0)])
    (ev,) = db.runaway()
    assert ev["RULE_ID"] == _AI and ev["SEVERITY"] == "HIGH" and ev["COMPANY"] == "ALFA"
    # a FLOAT renders '15' in Snowflake and '15.0' in sqlite: the number format is the engine's, the text is ours
    assert re.fullmatch(rf"ALICE used 44\.0 AI credits on {_day(-1)} \(~\$97\): 2\.9x the 15(\.0)?-credit daily cap",
                        ev["TITLE"]), ev["TITLE"]
    assert ev["TITLE"].count("$") == 1
    assert ev["METRIC_VALUE"] == pytest.approx(round(44.0 / 15, 4))           # the cap multiple (tuning units)
    assert ev["DEDUPE_KEY"] == f"{_AI}|ALICE|{_day(-1)}"
    assert ev["DETAIL"].startswith("Robust z ") and "(bar 3.5, AI_RUNAWAY_ROBUST_Z)" in ev["DETAIL"]
    assert "over 15 active day(s) in the 90 days before" in ev["DETAIL"]      # 15 history days, the scored day out
    assert re.search(r"Cap: above 30(\.0)? credits \(THRESHOLD_NUM x COCO_DAILY_CAP_CREDITS 15(\.0)?\)\. "
                     r"Sources: Snowsight\.", ev["DETAIL"]), ev["DETAIL"]
    assert "About 97 USD at 2.2 USD/credit" in ev["DETAIL"]
    assert "Cost Intelligence > Chargeback & AI > AI users" in ev["DETAIL"]
    assert db.runaway() == []                                                  # a re-run inserts nothing


def test_habitual_heavy_user_over_the_cap_but_under_the_z_bar_stays_quiet():
    history = _steady("DAVE", [14.0, 17.0, 20.0, 23.0, 26.0, 18.0, 22.0])
    db = _Db().ai([*history, (_day(-1), "DAVE", 31.0)])
    assert db.runaway() == []                                                  # 31 > 30 but z ~ 2.5 < 3.5
    # the same user at a real outlier raises
    db = _Db().ai([*history, (_day(-1), "DAVE", 60.0)])
    assert [e["DEDUPE_KEY"] for e in db.runaway()] == [f"{_AI}|DAVE|{_day(-1)}"]


def test_onset_user_with_fewer_than_5_prior_active_days_raises_on_the_cap_alone():
    db = _Db().ai([(_day(-20), "EVE", 1.0), (_day(-10), "EVE", 2.0), (_day(-5), "EVE", 1.5), (_day(-4), "EVE", 1.0),
                   (_day(-2), "EVE", 35.0)])
    (ev,) = db.runaway()
    assert ev["DETAIL"].startswith("No baseline yet: 4 active day(s) in the 90 days before, fewer than 5, so the cap "
                                   "alone raised this")
    assert ev["COMPANY"] == "ALL"                                              # unmapped -> the account-level route
    # a brand-new user (no history at all) too; under the cap, nothing
    db = _Db().ai([(_day(-1), "NEWBIE", 31.0), (_day(-1), "SMALL", 29.0)])
    assert [e["DEDUPE_KEY"] for e in db.runaway()] == [f"{_AI}|NEWBIE|{_day(-1)}"]
    # 5 prior active days = a baseline: z decides
    db = _Db().ai([*[(_day(-k), "FIVE", 20.0 + k) for k in range(5, 10)], (_day(-1), "FIVE", 31.0)])
    assert db.runaway() == []


def test_account_unknown_and_functions_rows_are_excluded_until_a_user_is_named():
    rows = [(_day(-1), "ACCOUNT", 99.0, "Functions"), (_day(-1), "UNKNOWN", 99.0, "Snowsight"),
            (_day(-1), "BOB", 5.0, "Snowsight"), (_day(-1), "BOB", 40.0, "Functions")]
    assert _Db().ai(rows).runaway() == []                                      # default: Functions off
    for off in ("FALSE", "no", "garbage"):
        assert _Db(settings={"AI_RUNAWAY_INCLUDE_FUNCTIONS": off}).ai(rows).runaway() == []
    on = _Db(settings={"AI_RUNAWAY_INCLUDE_FUNCTIONS": "TRUE"}).ai(rows).runaway()
    assert [e["DEDUPE_KEY"] for e in on] == [f"{_AI}|BOB|{_day(-1)}"]         # ACCOUNT stays out even then
    assert "Sources: Functions+Snowsight." in on[0]["DETAIL"]
    # the app-only 'UNKNOWN (<id>)' name is not the mart's UNKNOWN (the loader writes plain UNKNOWN)
    assert [e["DEDUPE_KEY"] for e in _Db().ai([(_day(-1), "UNKNOWN (123)", 50.0)]).runaway()] == [
        f"{_AI}|UNKNOWN (123)|{_day(-1)}"]


def test_a_multi_source_day_is_summed():
    db = _Db().ai([(_day(-2), "CAROL", 20.0, "Snowsight"), (_day(-2), "CAROL", 15.0, "CLI")])
    (ev,) = db.runaway()
    assert ev["TITLE"].startswith("CAROL used 35.0 AI credits") and "Sources: CLI+Snowsight." in ev["DETAIL"]
    assert _Db().ai([(_day(-2), "CAROL", 20.0, "Snowsight"), (_day(-2), "CAROL", 9.0, "CLI")]).runaway() == []


def test_only_the_last_3_complete_days_are_scored():
    rows = [(_day(k), f"U{-k}", 50.0) for k in (-5, -4, -3, -2, -1, 0)]
    keys = sorted(e["DEDUPE_KEY"] for e in _Db().ai(rows).runaway())
    assert keys == sorted(f"{_AI}|U{-k}|{_day(k)}" for k in (-3, -2, -1))     # not T-4, not today (partial)


def test_a_late_mart_day_raises_once_on_the_next_run():
    db = _Db().ai(_steady("LATE", [1.0, 1.2, 0.8, 1.1, 0.9]))
    assert db.runaway() == []                                                  # the mart has not loaded T-1 yet
    db.ai([(_day(-1), "LATE", 40.0)])                                          # the sibling mart task lands
    db.today = _TODAY + timedelta(days=1)                                      # next morning: T-1 is now T-2
    assert [e["DEDUPE_KEY"] for e in db.runaway()] == [f"{_AI}|LATE|{_day(-1)}"]
    assert db.runaway() == []
    db.today = _TODAY + timedelta(days=2)
    assert db.runaway() == []                                                  # still in the window, deduped


def test_the_scored_day_is_never_its_own_baseline_and_history_is_90_days():
    # 5 prior active days: the scored day is not a sixth (the onset test shows 4 + the scored day is no baseline)
    five = [(_day(k), "FIVE", v) for k, v in zip(range(-10, -5), [1.0, 1.1, 0.9, 1.2, 1.0], strict=True)]
    (ev,) = _Db().ai([*five, (_day(-1), "FIVE", 40.0)]).runaway()
    assert "over 5 active day(s) in the 90 days before" in ev["DETAIL"]
    # the window is [scored day - 90, scored day): day -90 counts, day -91 does not
    inside = [(_day(-1 - 90 + k), "EDGE_IN", 1.0 + k / 10) for k in range(5)]         # -91 .. -87
    outside = [(_day(-1 - 91 + k), "EDGE_OUT", 1.0 + k / 10) for k in range(5)]       # -92 .. -88: 4 inside
    got = {e["DEDUPE_KEY"].split("|")[1]: e["DETAIL"] for e in
           _Db().ai([*inside, *outside, (_day(-1), "EDGE_IN", 40.0), (_day(-1), "EDGE_OUT", 40.0)]).runaway()}
    assert got["EDGE_IN"].startswith("Robust z ") and "over 5 active day(s)" in got["EDGE_IN"]
    assert got["EDGE_OUT"].startswith("No baseline yet: 4 active day(s)")
    # history only > 90 days before the scored day: no baseline, the cap decides
    old = [(_day(-120 + k), "OLDIE", 40.0) for k in range(10)]
    (ev,) = _Db().ai([*old, (_day(-1), "OLDIE", 40.0)]).runaway()
    assert ev["DETAIL"].startswith("No baseline yet: 0 active day(s)")
    # a user who ran at 40 every day for 60 days: the scored 40 sits on its own median (z 0) -> quiet
    same = [(_day(-k), "STEADY", 40.0 + (k % 3)) for k in range(1, 61)]
    assert _Db().ai(same).runaway() == []


def test_mad_zero_falls_back_to_mean_absolute_deviation_then_to_999():
    mostly_flat = [(_day(-k), "FLAT", v) for k, v in zip(range(5, 14), [2, 2, 2, 2, 2, 2, 2, 5, 8], strict=True)]
    (ev,) = _Db().ai([*mostly_flat, (_day(-1), "FLAT", 31.0)]).runaway()     # MAD 0, MEAN_AD 1 -> z = 0.7979 * 29
    assert ev["DETAIL"].startswith("Robust z 23.1 ")
    all_flat = [(_day(-k), "FLAT2", 2.0) for k in range(5, 14)]
    (ev,) = _Db().ai([*all_flat, (_day(-1), "FLAT2", 31.0)]).runaway()       # MAD = MEAN_AD = 0 -> 999
    assert ev["DETAIL"].startswith("Robust z 999")


@pytest.mark.parametrize("cap", [None, "0", "-3", "abc", "", "15"])
def test_a_bad_or_missing_cap_reads_as_15(cap):
    settings = {} if cap is None else {"COCO_DAILY_CAP_CREDITS": cap}
    db = _Db(settings=settings).ai([(_day(-1), "HI", 30.5), (_day(-1), "LO", 29.5)])
    assert [e["DEDUPE_KEY"] for e in db.runaway()] == [f"{_AI}|HI|{_day(-1)}"]


def test_the_knobs_are_live():
    rows = [*_steady("K", [3.0, 3.5, 4.0, 2.5, 3.2]), (_day(-1), "K", 31.0)]
    assert len(_Db().ai(rows).runaway()) == 1
    assert _Db(settings={"COCO_DAILY_CAP_CREDITS": "20"}).ai(rows).runaway() == []       # needs > 40
    assert _Db(rules={_AI: ("HIGH", 3.0, 1)}).ai(rows).runaway() == []                   # needs > 45
    assert len(_Db(rules={_AI: ("HIGH", None, 1)}).ai(rows).runaway()) == 1              # NULL multiple -> 2
    assert _Db(rules={_AI: ("HIGH", 2.0, 0)}).ai(rows).runaway() == []                   # disabled
    assert _Db(settings={"AI_RUNAWAY_ROBUST_Z": "1000"}).ai(rows).runaway() == []        # z bar out of reach
    assert len(_Db(settings={"AI_RUNAWAY_ROBUST_Z": "x"}).ai(rows).runaway()) == 1       # junk -> 3.5
    (ev,) = _Db(rules={_AI: ("MEDIUM", 2.0, 1)}).ai(rows).runaway()
    assert ev["SEVERITY"] == "MEDIUM"                                                    # c.SEVERITY, never forced


def test_company_is_the_users_or_all_when_unknown():
    rows = [(_day(-1), "TREX_USER", 50.0), (_day(-1), "ALFA_USER", 50.0), (_day(-1), "NOBODY", 50.0)]
    db = _Db(companies={"TREX_USER": "Trexis", "ALFA_USER": "ALFA"}).ai(rows)
    got = {e["DEDUPE_KEY"].split("|")[1]: e["COMPANY"] for e in db.runaway()}
    assert got == {"TREX_USER": "Trexis", "ALFA_USER": "ALFA", "NOBODY": "ALL"}


def test_widths_hold_for_a_200_character_user_name():
    user = "u." * 100                                                         # 200 chars, dotted
    (ev,) = _Db().ai([(_day(-1), user, 123456.789)]).runaway()               # the CHECKs would reject an overflow
    assert len(ev["DEDUPE_KEY"]) == len(f"{_AI}|") + 200 + 11 <= 300
    assert len(ev["TITLE"]) <= 300 and ev["TITLE"].startswith(user + " used 123456.8 AI credits")


# ============================================================================================================
# [29] SEC_TRUST_REGRESSION
# ============================================================================================================
def _keys(events: list[dict]) -> list[str]:
    return [e["DEDUPE_KEY"] for e in events]


def test_trust_regression_fires_once_per_scanner_day():
    db = _Db().trust([(_day(-2), "S1", "HIGH", 3), (_day(-1), "S1", "HIGH", 5), (_day(0), "S1", "HIGH", 5)])
    (ev,) = db.regression()
    assert ev["DEDUPE_KEY"] == f"{_TRUST}|S1|{_day(-1)}" and ev["COMPANY"] == "ALL" and ev["SEVERITY"] == "HIGH"
    assert ev["METRIC_VALUE"] == 2 and ev["TITLE"] == (
        f"Trust Center HIGH scanner regressed: Scanner S1 at-risk entities 3 -> 5 on {_day(-1)}")
    assert ev["DETAIL"].startswith(f"Scanner S1: 5 entities at risk in the {_day(-1)} snapshot (latest scan "
                                   f"{_day(-1)} 06:10), up from 3 on {_day(-2)}. Security > Trust Center")
    assert "The previous day read 0" not in ev["DETAIL"]
    assert db.regression() == []


def test_trust_first_snapshot_improvements_and_low_severities_never_raise():
    db = _Db().trust([(_day(0), "NEW", "CRITICAL", 7),                                   # first-ever snapshot
                      (_day(-1), "BETTER", "HIGH", 5), (_day(0), "BETTER", "HIGH", 2),
                      (_day(-1), "SAME", "CRITICAL", 4), (_day(0), "SAME", "CRITICAL", 4),
                      (_day(-1), "MED", "MEDIUM", 1), (_day(0), "MED", "MEDIUM", 9),
                      (_day(-1), "LOWS", "LOW", 1), (_day(0), "LOWS", "LOW", 9),
                      (_day(-6), "OLDRISE", "HIGH", 1), (_day(-5), "OLDRISE", "HIGH", 4),       # too old
                      (_day(-1), "OLDRISE", "HIGH", 4), (_day(0), "OLDRISE", "HIGH", 4)])
    assert db.regression() == []


def test_trust_case_threshold_gap_and_name_edges():
    db = _Db().trust([(_day(-1), "LC", "high", 1), (_day(0), "LC", "high", 2),              # lowercase severity
                      (_day(-5), "GAP", "CRITICAL", 2), (_day(-1), "GAP", "CRITICAL", 4)])   # previous day 4 days back
    got = {e["DEDUPE_KEY"]: e for e in db.regression()}
    assert set(got) == {f"{_TRUST}|LC|{_day(0)}", f"{_TRUST}|GAP|{_day(-1)}"}
    assert f"up from 2 on {_day(-5)}" in got[f"{_TRUST}|GAP|{_day(-1)}"]["DETAIL"]
    # a NULL scanner name falls back to the id; threshold 2 ignores a rise of 1
    db = _Db(rules={_TRUST: ("HIGH", 2.0, 1)})
    db.con.executemany("INSERT INTO SECURITY_TRUST_SNAPSHOT VALUES (?, ?, NULL, 'HIGH', ?, NULL)",
                       [(_day(-1), "X", 1), (_day(0), "X", 3), (_day(-1), "Y", 1), (_day(0), "Y", 2)])
    (ev,) = db.regression()
    assert ev["TITLE"] == f"Trust Center HIGH scanner regressed: X at-risk entities 1 -> 3 on {_day(0)}"
    assert "(latest scan unknown)" in ev["DETAIL"]


def _delta_view_sql() -> str:
    body = _V075[_V075.index("CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.V_SECURITY_TRUST_DELTA AS\n"):]
    body = body[body.index("WITH ranked AS"):body.index(";\n")]
    return body.replace("DBA_MAINT_DB.OVERWATCH.", "")


def test_a_rise_after_the_morning_scan_is_caught_next_morning_where_the_delta_view_misses_it():
    db = _Db(today=_TODAY - timedelta(days=1))
    db.trust([(_day(-2), "LATE", "HIGH", 3), (_day(-1), "LATE", "HIGH", 3)])     # D's 07:00 view: no change
    assert db.regression() == []
    db.con.execute("UPDATE SECURITY_TRUST_SNAPSHOT SET TOTAL_AT_RISK_COUNT = 6 WHERE DAY = ?", (_day(-1),))
    db.today = _TODAY                                                             # D+1 07:00
    db.trust([(_day(0), "LATE", "HIGH", 6)])
    assert _keys(db.regression()) == [f"{_TRUST}|LATE|{_day(-1)}"]
    # the view compares only the two newest days: D+1 (6) vs D (6) -> UNCHANGED, the rise is invisible
    (state,) = db.con.execute(f"SELECT CHANGE_STATE FROM ({_delta_view_sql()}) WHERE SCANNER_ID = 'LATE'").fetchall()
    assert state == ("UNCHANGED",)


def test_a_rise_before_the_morning_scan_raises_today_and_is_not_repeated_tomorrow():
    db = _Db().trust([(_day(-1), "EARLY", "CRITICAL", 3), (_day(0), "EARLY", "CRITICAL", 6)])
    assert _keys(db.regression()) == [f"{_TRUST}|EARLY|{_day(0)}"]
    db.today = _TODAY + timedelta(days=1)
    db.trust([(_day(1), "EARLY", "CRITICAL", 6)])
    assert db.regression() == []                                                  # yesterday's rise: same key


def test_flapping_re_raises_and_a_return_from_zero_is_flagged():
    db = _Db().trust([(_day(-3), "FLAP", "HIGH", 3), (_day(-2), "FLAP", "HIGH", 5)])
    db.today = _TODAY - timedelta(days=1)
    assert _keys(db.regression()) == [f"{_TRUST}|FLAP|{_day(-2)}"]
    db.trust([(_day(-1), "FLAP", "HIGH", 2), (_day(0), "FLAP", "HIGH", 5)])
    db.today = _TODAY
    assert _keys(db.regression()) == [f"{_TRUST}|FLAP|{_day(0)}"]
    # the loader's synthetic 0 row for a scanner missing from FINDINGS: its return reads as a rise, and says so
    db = _Db().trust([(_day(-2), "GONE", "HIGH", 4), (_day(-1), "GONE", "HIGH", 0), (_day(0), "GONE", "HIGH", 4)])
    (ev,) = db.regression()
    assert ev["DEDUPE_KEY"] == f"{_TRUST}|GONE|{_day(0)}"
    assert "The previous day read 0: the loader books a scanner missing from FINDINGS as 0" in ev["DETAIL"]


# ============================================================================================================
# [07] SEC_FAILED_LOGINS wording
# ============================================================================================================
def test_failed_logins_title_and_detail_branch_on_successes():
    db = _Db().logins([(_day(0), "LOCKED", 12, 12), (_day(0), "GOTIN", 14, 12), (_day(-1), "NULLS", None, 11),
                       (_day(0), "UNDER", 30, 9)])
    got = {e["DEDUPE_KEY"]: e for e in db.run(_ARM07)}
    assert set(got) == {f"SEC_FAILED_LOGINS|LOCKED|{_day(0)}", f"SEC_FAILED_LOGINS|GOTIN|{_day(0)}",
                        f"SEC_FAILED_LOGINS|NULLS|{_day(-1)}"}                  # key and predicate unchanged
    locked, gotin, nulls = (got[f"SEC_FAILED_LOGINS|{u}|{d}"] for u, d in
                            (("LOCKED", _day(0)), ("GOTIN", _day(0)), ("NULLS", _day(-1))))
    assert locked["TITLE"] == f"LOCKED had 12 failed logins on {_day(0)} and no successful login"
    assert gotin["TITLE"] == f"GOTIN had 12 failed logins on {_day(0)}, 2 successful"
    assert nulls["TITLE"] == f"NULLS had 11 failed logins on {_day(-1)} and no successful login"
    assert locked["DETAIL"].startswith("No successful login that day: most likely a lockout")
    assert gotin["DETAIL"].startswith("The same day also had successful logins. A failed burst followed within "
                                      "60 minutes by a success raises SEC_LOGIN_TAKEOVER from the hourly scan")
    for ev in got.values():
        assert ev["DETAIL"].endswith("Review Security > Access > Authentication: failed-login reasons and client IPs.")
        assert ev["SEVERITY"] == "HIGH" and ev["METRIC_VALUE"] in (11, 12)
    assert db.run(_ARM07) == []
    # a 200-character user name still fits TITLE
    long_user = "x" * 200
    (ev,) = _Db().logins([(_day(0), long_user, 999999999999, 999999999998)]).run(_ARM07)
    assert len(ev["TITLE"]) <= 300


# ============================================================================================================
# The translator has teeth
# ============================================================================================================
def test_the_translator_fails_closed():
    with pytest.raises(AssertionError):
        _to_sqlite(_ARM28.replace(":ai_credit_price", ":not_a_bind"))
    with pytest.raises(AssertionError):
        _to_sqlite(_ARM28.replace("WITHIN GROUP (ORDER BY f.SOURCE)", "WITHIN GROUP (ORDER BY f.DAY)"))
    with pytest.raises(AssertionError):
        _to_sqlite(_ARM29.replace("        ) b (RULE_ID", "        ) bb (RULE_ID"))
    # and the harness runs the migration's own text: a mutated window changes the outcome
    rows = [(_day(-4), "W", 50.0)]
    assert _Db().ai(rows).runaway() == []
    db = _Db().ai(rows)
    db.con.execute(_to_sqlite(_ARM28.replace("DATEADD('day', -3, k.TODAY)", "DATEADD('day', -4, k.TODAY)")))
    assert db.con.execute("SELECT COUNT(*) FROM ALERT_EVENTS").fetchone()[0] == 1
