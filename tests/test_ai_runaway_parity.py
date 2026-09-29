"""Permanent parity lock (Next-Fifty #37): the per-user AI runaway arm of SP_ALERT_SCAN_DAILY (V163 arm [28],
COST_AI_USER_RUNAWAY) vs its app twin, ``app.logic.quotas.runaway_days`` (the "runaway-rule days" of the
suggested-quota table on Cost Intelligence > Chargeback & AI).

The arm is read from the LATEST definer body across every V*.sql (last CREATE OR REPLACE wins, as on the live
account), so a later re-derivation that drifts from the app -- or an app change that drifts from the arm --
fails here until both move together. Three layers:
  1. EXECUTED parity: the arm's own INSERT text runs in sqlite (the house translation of
     tests/migrations/test_v160_sleep_polling_harness, plus MEDIAN, TRY_TO_BOOLEAN, LISTAGG(DISTINCT) and
     COMPANY_FOR_USER shims) over one synthetic FACT_AI_USAGE_DAILY, and must raise exactly the user-days
     ``runaway_days`` returns for the same rows, with METRIC_VALUE == CAP_MULTIPLE and the "No baseline yet"
     DETAIL exactly on the no-baseline days -- across the setting fallbacks (cap '0' / '-3' / junk / absent,
     z junk / tuned, THRESHOLD_NUM NULL / tuned, the Functions switch).
  2. Literal parity: 0.6745 / 0.7979, the 90-day baseline, the < 5 onset, the 3-day scored window, the 93-day
     read, the 3.5 / 15 / 2 fallbacks, the not-a-user names and the METRIC_VALUE rounding are the quotas /
     anomaly constants.
  3. Seed parity: the V163 ALERT_CONFIG THRESHOLD_NUM seed (2) and SETTINGS seeds ('3.5', 'FALSE') are the
     Python defaults, and so is config.DEFAULT_SETTINGS.

The arm ships with V163 (wave 4 slice S2); until that migration is in the tree these tests fail loudly by
design -- the lock must never silently skip.

The mart never holds the live frame's 'UNKNOWN (<id>)' placeholder (the loader writes plain 'UNKNOWN'), so
the fixture has none: user_day_totals drops it on the app side only.
"""

from __future__ import annotations

import functools
import re
import sqlite3
import statistics
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app import config
from app.logic import anomaly, quotas
from tests._source import ROOT, read
from tests.migrations import test_v160_sleep_polling_harness as h

_PROC = "SP_ALERT_SCAN_DAILY"
_RULE = "COST_AI_USER_RUNAWAY"
_ARM_MARK = re.compile(r"\n[ ]*-- \[28\] COST_AI_USER_RUNAWAY\b")
_TODAY = date(2026, 9, 29)                     # Central TODAY of the daily scan in the fixture
_AI_PRICE = 2.20


@functools.cache
def _body() -> str:
    from tests.test_alert_rule_consistency import _latest_proc_bodies

    bodies = _latest_proc_bodies()
    assert _PROC in bodies, f"{_PROC} has no definer in snowflake/migrations"
    return bodies[_PROC]


def _arm() -> str:
    """Arm [28]'s text: its '-- [28] COST_AI_USER_RUNAWAY' comment up to the next '-- [NN]' arm comment."""
    body = _body()
    m = _ARM_MARK.search(body)
    assert m, (f"arm [28] {_RULE} is not in the latest {_PROC} body -- it ships with V163 "
               "(Next-Fifty wave 4 slice S2); this parity lock stays red until it does")
    nxt = re.compile(r"\n[ ]*-- \[\d+\] ").search(body, m.end())
    return body[m.start():nxt.start() if nxt else len(body)]


def _insert_sql() -> str:
    """The arm's one INSERT INTO ALERT_EVENTS statement (comments dropped, literals raw)."""
    arm = _arm()
    i = arm.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")
    j = arm.index("\n    EXCEPTION", i)
    stmts = h._statements(arm[i:j])
    assert len(stmts) == 1, [s[:60] for _, s in stmts]
    return stmts[0][1]


def _declared_binds() -> dict[str, object]:
    """Every DECLAREd scripting variable of the latest body (the INSERT may bind any of them)."""
    m = re.search(r"^DECLARE\n(.*?)^BEGIN\n", _body(), re.M | re.S)
    assert m, "no DECLARE ... BEGIN section in the latest body"
    names = re.findall(r"^\s+(\w+)\s+[A-Z]", m.group(1), re.M)
    env: dict[str, object] = {n.upper(): None for n in names}
    env.update({"AI_CREDIT_PRICE": _AI_PRICE, "CREDIT_PRICE": 3.68, "BUDGET_USD": 0.0, "FAILS": 0})
    return env


# ============================================================================================================
# sqlite: the harness translation + the shims arm [28] needs on top of it
# ============================================================================================================
class _Median:
    def __init__(self) -> None:
        self.v: list[float] = []

    def step(self, x) -> None:
        if x is not None:
            self.v.append(float(x))

    def finalize(self):
        return statistics.median(self.v) if self.v else None


class _ListAggDistinct:
    def __init__(self) -> None:
        self.v: set[str] = set()
        self.sep = ""

    def step(self, x, sep) -> None:
        self.sep = sep
        if x is not None:
            self.v.add(str(x))

    def finalize(self):
        return self.sep.join(sorted(self.v)) if self.v else None


_TRUE = {"TRUE", "T", "YES", "Y", "ON", "1"}
_FALSE = {"FALSE", "F", "NO", "N", "OFF", "0"}


def _try_to_boolean(v):
    """Snowflake TRY_TO_BOOLEAN on a VARCHAR: the documented true/false spellings, else NULL."""
    if v is None:
        return None
    s = str(v).strip().upper()
    return 1 if s in _TRUE else 0 if s in _FALSE else None


def _to_varchar(x, fmt=None):
    if fmt is not None:
        assert fmt == "YYYY-MM-DD", fmt          # the only format a day key may use
        return str(x)[:10]
    return h._to_varchar(x)


# COMPANY_FOR_USER (V044) never returns NULL: a mapped company, else 'UNKNOWN'.
_USER_COMPANY = {"TRX_HEAVY": "Trexis", "TRX_SPIKE": "Trexis"}


def _company_for_user(u):
    s = str(u or "")
    return _USER_COMPANY.get(s) or ("ALFA" if s.startswith("A_") else "UNKNOWN")


def _translate(sql: str) -> str:
    sql = re.sub(r"LISTAGG\(DISTINCT ([\w.]+), ('[^']*')\)\s*WITHIN GROUP\s*\(ORDER BY \1\)",
                 r"LISTAGG_DISTINCT(\1, \2)", sql)
    return h._to_sqlite(sql, _declared_binds())


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute("PRAGMA case_sensitive_like = ON")
    pure = {
        "DATEADD": (3, h._dateadd), "TO_VARCHAR": (-1, _to_varchar), "TRY_TO_DOUBLE": (1, h._try_to_double),
        "TRY_TO_BOOLEAN": (1, _try_to_boolean), "LEFT": (2, h._nullsafe(lambda s, n: str(s)[:int(n)])),
        "IFF": (3, lambda c, a, b: a if c else b),
        "GREATEST": (-1, h._nullsafe(lambda *a: max(a))), "LEAST": (-1, h._nullsafe(lambda *a: min(a))),
        "COMPANY_FOR_USER": (1, _company_for_user),
    }
    for name, (narg, fn) in pure.items():
        con.create_function(name, narg, fn, deterministic=True)
    con.create_function("TODAY_D", 0, lambda: _TODAY.isoformat())
    con.create_function("NOW_TS", 0, lambda: f"{_TODAY.isoformat()} 07:05:00")
    con.create_aggregate("MEDIAN", 1, _Median)
    con.create_aggregate("LISTAGG_DISTINCT", 2, _ListAggDistinct)
    con.create_aggregate("LISTAGG_ORD", -1, h._ListAggOrd)
    con.create_aggregate("COUNT_IF", 1, h._CountIf)
    con.execute("CREATE TABLE FACT_AI_USAGE_DAILY (DAY TEXT, USER_NAME TEXT, SOURCE TEXT, MODEL_NAME TEXT, "
                "EMAIL TEXT, REQUESTS INT, TOKENS INT, CREDITS REAL)")
    con.execute(f"CREATE TABLE SETTINGS ({h._SCHEMAS['SETTINGS']})")
    con.execute(f"CREATE TABLE ALERT_CONFIG ({h._SCHEMAS['ALERT_CONFIG']})")
    con.execute(f"CREATE TABLE ALERT_EVENTS ({h._SCHEMAS['ALERT_EVENTS']})")
    return con


# ============================================================================================================
# The fixture: one synthetic mart, the same rows for SQL and pandas
# ============================================================================================================
def _fixture() -> list[tuple[str, str, str, float]]:
    """(DAY, USER_NAME, SOURCE, CREDITS) rows -- random steady users plus every edge the arm decides."""
    rng = np.random.default_rng(37)
    rows: list[tuple[str, str, str, float]] = []

    def put(k: int, user: str, credits: float, source: str = "Snowsight") -> None:
        rows.append(((_TODAY - timedelta(days=k)).isoformat(), user, source, float(credits)))

    for user, base, p in (("A_ALICE", 3.0, 0.7), ("A_BOB", 12.0, 0.6), ("A_CAROL", 0.5, 0.8),
                          ("TRX_HEAVY", 22.0, 0.9), ("A_SPARSE", 4.0, 0.08)):
        for k in range(5, 131):               # the random history stops before the scored window
            if rng.random() < p:
                put(k, user, max(0.05, rng.normal(base, base * 0.25)))
    put(1, "A_ALICE", 44.0, "CLI")            # a multi-source spike day: summed with any Snowsight row
    put(2, "A_BOB", 70.0, "CLI")              # a clear runaway against a ~12/day habit
    put(3, "TRX_HEAVY", 31.5)                 # a habitual 22/day user above 2x cap but not a z outlier
    put(2, "A_EVE", 35.0, "CLI")              # onset: no history at all -> the cap leg decides
    for k in (40, 30, 20):
        put(k, "A_NEWISH", 2.0)               # 3 prior active days (< 5) -> still no baseline
    put(1, "A_NEWISH", 31.0)
    for k in range(10, 40, 3):
        put(k, "A_FLAT", 5.0)                 # MAD = 0 and mean-AD = 0 -> 999 above the median
    put(1, "A_FLAT", 33.0)
    for k in range(10, 40, 3):
        put(k, "TRX_SPIKE", 5.0 if k % 2 else 6.0)
    put(3, "TRX_SPIKE", 46.0)                 # scored day at the window's oldest edge (TODAY-3)
    put(4, "A_BOB", 90.0)                     # TODAY-4: outside the scored window, inside the baseline
    put(0, "A_BOB", 95.0)                     # TODAY: the partial day, never scored
    put(1, "A_EXACT", 30.0)                   # exactly 2x the cap: not strictly above -> never raised
    put(1, "Z_ORPHAN", 40.0, "CLI")           # onset, and COMPANY_FOR_USER says 'UNKNOWN' -> COMPANY 'ALL'
    put(1, "A_DAN", 20.0, "Functions")        # a Functions row naming a user: counts only with the switch
    put(1, "A_DAN", 12.0, "CLI")
    put(1, "ACCOUNT", 99.0, "Functions")      # the loader's account-level Functions row: never a user
    put(2, "UNKNOWN", 88.0, "CLI")            # no USERS match: never a user
    put(1, "A_ZERO", 0.0, "CLI")              # a zero-credit day is not an active day
    return rows


def _frame(rows) -> pd.DataFrame:
    return pd.DataFrame([{"USER_NAME": u, "SOURCE": s, "USAGE_DATE": date.fromisoformat(d), "CREDITS": c}
                         for d, u, s, c in rows])


# (id, COCO cap SETTING, AI_RUNAWAY_ROBUST_Z SETTING, THRESHOLD_NUM, AI_RUNAWAY_INCLUDE_FUNCTIONS SETTING)
_CASES = [
    ("seeds", "15.0", "3.5", 2, "FALSE"),
    ("cap-zero", "0", "3.5", 2, "FALSE"),
    ("cap-negative", "-3", None, 2, "FALSE"),
    ("cap-junk-z-junk", "abc", "x", 2, "FALSE"),
    ("cap-absent", None, "3.5", None, "FALSE"),
    ("tuned", "12", "2", 1.5, "FALSE"),
    ("functions-on", "15.0", "3.5", 2, "TRUE"),
    ("functions-yes", "15.0", "3.5", 2, "yes"),
]


def _run_arm(cap, z, thr, incl) -> dict[tuple[str, str], dict]:
    con = _connect()
    try:
        con.executemany("INSERT INTO FACT_AI_USAGE_DAILY (DAY, USER_NAME, SOURCE, MODEL_NAME, CREDITS) "
                        "VALUES (?, ?, ?, 'n/a', ?)", _fixture())
        for key, val in (("COCO_DAILY_CAP_CREDITS", cap), ("AI_RUNAWAY_ROBUST_Z", z),
                         ("AI_RUNAWAY_INCLUDE_FUNCTIONS", incl), ("AI_CREDIT_PRICE_USD", str(_AI_PRICE))):
            if val is not None:
                con.execute("INSERT INTO SETTINGS VALUES (?, ?)", (key, val))
        con.execute("INSERT INTO ALERT_CONFIG (RULE_ID, SEVERITY, ENABLED, THRESHOLD_NUM, AUTO_CLEAR_ENABLED) "
                    "VALUES (?, 'HIGH', 1, ?, 0)", (_RULE, thr))
        sql = _translate(_insert_sql())
        first = con.execute(sql).rowcount
        again = con.execute(sql).rowcount
        cur = con.execute("SELECT * FROM ALERT_EVENTS WHERE RULE_ID = ?", (_RULE,))
        cols = [c[0] for c in cur.description]
        events = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        con.close()
    assert first == len(events) and again == 0, (first, again, len(events))   # one event per user-day, once
    out: dict[tuple[str, str], dict] = {}
    for e in events:
        m = re.fullmatch(rf"{_RULE}\|(.+)\|(\d{{4}}-\d{{2}}-\d{{2}})", e["DEDUPE_KEY"])
        assert m, e["DEDUPE_KEY"]
        out[(m.group(1), m.group(2))] = e
    return out


def _run_python(cap, z, thr, incl) -> dict[tuple[str, str], dict]:
    totals = quotas.user_day_totals(_frame(_fixture()), include_functions=_try_to_boolean(incl) == 1)
    got = quotas.runaway_days(totals, cap, thr, z, today=_TODAY)
    return {(r["USER_NAME"], r["DAY"].isoformat()): r for r in got.to_dict("records")}


# ============================================================================================================
# 1. executed parity
# ============================================================================================================
@pytest.mark.parametrize(("cap", "z", "thr", "incl"), [c[1:] for c in _CASES], ids=[c[0] for c in _CASES])
def test_arm_and_runaway_days_raise_the_same_user_days(cap, z, thr, incl):
    sql, py = _run_arm(cap, z, thr, incl), _run_python(cap, z, thr, incl)
    assert sorted(sql) == sorted(py)
    for key, event in sql.items():
        row = py[key]
        assert event["METRIC_VALUE"] == pytest.approx(row["CAP_MULTIPLE"], abs=1e-9), key
        assert ("No baseline yet" in (event["DETAIL"] or "")) == bool(row["NO_BASELINE"]), key
        assert event["SEVERITY"] == "HIGH"                              # c.SEVERITY, never a literal CRITICAL
        assert event["COMPANY"] in ("ALFA", "Trexis", "ALL"), event["COMPANY"]   # never 'UNKNOWN'


def test_the_fixture_exercises_every_branch():
    """Guard against a vacuous parity: under the seeds the fixture raises a z-outlier, an onset user, a
    no-dispersion history and a Trexis user, and holds back the habitual heavy user and the edges."""
    py = _run_python("15.0", "3.5", 2, "FALSE")
    days = {(u, d): r for (u, d), r in py.items()}
    d = {k: (_TODAY - timedelta(days=k)).isoformat() for k in range(5)}
    assert ("A_BOB", d[2]) in days and not days[("A_BOB", d[2])]["NO_BASELINE"]     # z outlier
    assert days[("A_EVE", d[2])]["NO_BASELINE"] and days[("A_NEWISH", d[1])]["NO_BASELINE"]
    assert days[("A_FLAT", d[1])]["ROBUST_Z"] == anomaly.NO_DISPERSION_Z
    assert ("TRX_SPIKE", d[3]) in days
    for held in (("TRX_HEAVY", d[3]), ("A_BOB", d[4]), ("A_BOB", d[0]), ("A_EXACT", d[1]),
                 ("A_DAN", d[1]), ("ACCOUNT", d[1]), ("UNKNOWN", d[2]), ("A_ZERO", d[1])):
        assert held not in days, held
    # the Functions switch adds A_DAN's 20 + 12 = 32 credits (> 30) on the same day
    assert ("A_DAN", d[1]) in _run_python("15.0", "3.5", 2, "TRUE")


def test_arm_company_is_the_users_company_else_all():
    """Owner 2026-09-29: COALESCE(NULLIF(COMPANY_FOR_USER(user), 'UNKNOWN'), 'ALL') -- a Trexis user's runaway
    stays Trexis-scoped, an unattributable one is account-level and reaches the ALFA-only Teams route."""
    d = {k: (_TODAY - timedelta(days=k)).isoformat() for k in range(5)}
    sql = _run_arm("15.0", "3.5", 2, "FALSE")
    assert sql[("TRX_SPIKE", d[3])]["COMPANY"] == "Trexis"
    assert sql[("A_BOB", d[2])]["COMPANY"] == "ALFA"
    assert _company_for_user("Z_ORPHAN") == "UNKNOWN" and sql[("Z_ORPHAN", d[1])]["COMPANY"] == "ALL"


# ============================================================================================================
# 2. literal parity (arm text vs the Python constants)
# ============================================================================================================
def test_arm_constants_are_the_python_constants():
    arm = h._lex(_arm())
    code = "".join(t if k == "code" else f"'{t}'" for k, t in arm if k != "comment")
    assert repr(anomaly._MAD_K) == "0.6745" and f"{anomaly._MAD_K} *" in code
    assert repr(anomaly._MEANAD_K) == "0.7979" and f"{anomaly._MEANAD_K} *" in code
    assert f"DATEADD('day', -{quotas.RUNAWAY_BASELINE_DAYS}, c.DAY)" in code
    assert re.findall(r"<\s*(\d+)\s+THEN\s+NULL", code) == [str(anomaly.ROBUST_Z_MIN_HISTORY)]
    assert f"IFF(c.CR > d.MED, {int(anomaly.NO_DISPERSION_Z)}, 0)" in code
    windows = {int(n) for n in re.findall(r"DATEADD\('day', -(\d+), \w+\.TODAY\)", code)}
    assert windows == {quotas.RUNAWAY_SCORED_DAYS, quotas.RUNAWAY_SCORED_DAYS + quotas.RUNAWAY_BASELINE_DAYS}
    thr = re.findall(r"COALESCE\(c\.THRESHOLD_NUM,\s*([\d.]+)\)", code)
    assert thr and {float(t) for t in thr} == {quotas.RUNAWAY_CAP_MULTIPLE}
    z = re.search(r"'AI_RUNAWAY_ROBUST_Z'.*?\)\)\),\s*([\d.]+)\)\s+AS\s+Z_MIN", code, re.S)
    assert z and float(z.group(1)) == quotas.RUNAWAY_ROBUST_Z
    cap = re.search(r"COALESCE\(NULLIF\(GREATEST\(COALESCE\(TRY_TO_DOUBLE\(MAX\(IFF\(KEY = "
                    r"'COCO_DAILY_CAP_CREDITS', VALUE, NULL\)\)\),\s*([\d.]+)\),\s*0\),\s*0\),\s*([\d.]+)\)", code)
    assert cap and float(cap.group(1)) == float(cap.group(2)) == quotas.DEFAULT_CAP_CREDITS
    names = re.search(r"USER_NAME NOT IN \(([^)]*)\)", code)
    assert names and tuple(re.findall(r"'([^']*)'", names.group(1))) == quotas.NOT_A_USER
    assert f"SOURCE <> '{quotas.FUNCTIONS_SOURCE}' OR n.INCL_FN" in code
    assert "ROUND(s.CR / n.CAP_CR, 4)" in code                          # METRIC_VALUE == CAP_MULTIPLE
    assert "s.Z IS NULL OR s.Z >= n.Z_MIN" in code                      # the onset rule (owner 2026-09-29)
    assert "h.DAY < c.DAY" in code                                      # the scored day is not its own baseline
    assert "'CRITICAL'" not in code


# ============================================================================================================
# 3. seed parity
# ============================================================================================================
def _migration_text() -> str:
    return "\n".join(read(p.relative_to(ROOT).as_posix())
                     for p in sorted((ROOT / "snowflake" / "migrations").glob("V*.sql")))


def test_seeds_are_the_python_defaults():
    text = _migration_text()
    seed = re.findall(rf"\('{_RULE}',\s*'COST',\s*'(?:[^']|'')*',\s*TRUE,\s*'HIGH',\s*([\d.]+),\s*\d+\)", text)
    assert seed, f"no ALERT_CONFIG seed tuple for {_RULE} (V163)"
    assert {float(s) for s in seed} == {quotas.RUNAWAY_CAP_MULTIPLE}
    z = re.findall(r"\('AI_RUNAWAY_ROBUST_Z',\s*'([\d.]+)'\)", text)
    assert z and {float(v) for v in z} == {quotas.RUNAWAY_ROBUST_Z}
    assert re.search(r"\('AI_RUNAWAY_INCLUDE_FUNCTIONS',\s*'FALSE'\)", text)
    assert float(config.DEFAULT_SETTINGS["AI_RUNAWAY_ROBUST_Z"]) == quotas.RUNAWAY_ROBUST_Z
    assert str(config.DEFAULT_SETTINGS["AI_RUNAWAY_INCLUDE_FUNCTIONS"]).upper() == "FALSE"
    assert float(config.DEFAULT_SETTINGS["COCO_DAILY_CAP_CREDITS"]) == quotas.DEFAULT_CAP_CREDITS
