"""Executed harness for V172 (the detection scans).

The migration's OWN SQL text -- never a hand-written twin -- runs in an in-memory sqlite:
  * the R1-124 HD template (every CASE block of both scans' VERDICT_DETAIL) against formulas.humanize_duration;
  * the step-5 AFTER credits/call derived table (R2-022 + R2-025: settled runs, per scheduled run, LEFT JOIN +
    HAVING, the R2-021 anchored match) and the collapsed step-3/4 TASK count legs (R2-025), each beside its V140
    twin to show what changed;
  * the repairs R1-R4 (company re-stamps from the FQN in DEDUPE_KEY part 2, the first-apply baseline null, the
    terminal-attempt TASK re-freeze), run twice to prove idempotence.

The translation is minimal and fails closed: FQNs dropped, the clock pinned (NOW_TS / TODAY_D), the :binds made
literals, ``x::INT`` -> CAST, ``x::DATE`` -> TO_DATE_S(x), ``UPDATE T t`` -> ``UPDATE T AS t``, QUALIFY -> a ranked
sub-select, COUNT_IF -> SUM(CASE), ``POSITION(a IN b)`` -> INSTR(b, a), ILIKE -> LIKE; ROUND(.., 'HALF_TO_EVEN'),
TO_VARCHAR, IFF, FLOOR, MOD, DATEADD, DATEDIFF, GREATEST, SPLIT_PART, ENDSWITH, REGEXP_REPLACE, MEDIAN,
APPROX_PERCENTILE and COMPANY_FOR_DATABASE (V044) shimmed with Snowflake semantics. Timestamps are
'YYYY-MM-DD HH:MM:SS' strings (they compare lexicographically).
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from tests._source import read

_MIG = read("snowflake/migrations/V172__detection_scans_company_and_accuracy.sql")
_V140 = read("snowflake/migrations/V140__change_impact_exclude_self_procs.sql")
_FMT = "%Y-%m-%d %H:%M:%S"
_NOW = datetime(2026, 10, 1, 7, 0, 0)                     # the 06:50 Central scan's morning


def _proc_body(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}(")
    o = text.index("$$", s) + 2
    return text[o:text.index("$$;", o)]


_CI = _proc_body(_MIG, "SP_CHANGE_IMPACT_SCAN")
_CI0 = _proc_body(_V140, "SP_CHANGE_IMPACT_SCAN")
_WH = _proc_body(_MIG, "SP_WAREHOUSE_CHANGE_SCAN")


def _nth(text: str, needle: str, n: int) -> int:
    pos = -1
    for _ in range(n + 1):
        pos = text.index(needle, pos + 1)
    return pos


def _between(text: str, start: str, end: str, n: int = 0) -> str:
    i = _nth(text, start, n)
    return text[i:text.index(end, i)]


# ============================================================================================================
# Snowflake -> sqlite
# ============================================================================================================
def _lex(sql: str) -> list[tuple[str, str]]:
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


def _cast_int(sql: str) -> str:
    """``f(...)::INT`` -> ``CAST(f(...) AS INTEGER)`` (walks back over the balanced call)."""
    while "::INT" in sql:
        k = sql.index("::INT")
        assert sql[k - 1] == ")", sql[k - 40:k + 5]
        depth, j = 0, k - 1
        while True:
            if sql[j] == ")":
                depth += 1
            elif sql[j] == "(":
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        m = re.search(r"[A-Za-z_]\w*$", sql[:j])
        start = m.start() if m else j
        sql = sql[:start] + "CAST(" + sql[start:k] + " AS INTEGER)" + sql[k + len("::INT"):]
    return sql


_QUALIFY_RE = re.compile(
    r"SELECT (?P<cols>[^()]*?)\n(?P<ind>\s*)FROM TASK_HISTORY\n\s*WHERE (?P<where>.*?)\n\s*"
    r"QUALIFY (?P<win>ROW_NUMBER\(\) OVER \(PARTITION BY [^)]*\)) = 1", re.S)


def _to_sqlite(sql: str, binds: dict | None = None) -> str:
    s = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    out = []
    for kind, text in _lex(s):
        if kind == "comment":
            continue
        if kind == "code":
            text = text.replace("CURRENT_TIMESTAMP()", "NOW_TS()").replace("CURRENT_DATE()", "TODAY_D()")
            for name, val in (binds or {}).items():
                text = re.sub(rf"(?<![:\w]):{name}\b", val, text)
            assert re.search(r"(?<![:\w]):[a-z_]", text) is None, text
            text = text.replace(" ILIKE ", " LIKE ")
            text = re.sub(r"\bUPDATE (\w+) (t)\n", r"UPDATE \1 AS \2\n", text)
        out.append(text)
    s = "".join(out)
    s = re.sub(r"(\w+\.\w+)::DATE\b", r"TO_DATE_S(\1)", s)
    s = _cast_int(s)
    s = re.sub(r"\bROUND\(", "HROUND(", s)
    s = re.sub(r"COUNT_IF\(([^()]*)\)", r"SUM(CASE WHEN \1 THEN 1 ELSE 0 END)", s)
    s = re.sub(r"POSITION\((.+?) IN\s+(REGEXP_REPLACE\(UPPER\(q\.QUERY_TEXT\), '\[\[:space:\]\]', ''\))\)",
               r"INSTR(\2, \1)", s, flags=re.S)
    s = re.sub(r"POSITION\((.+?) IN\s+(REPLACE\(REPLACE\(UPPER\(q\.QUERY_TEXT\), ' ', ''\), CHR\(10\), ''\))\)",
               r"INSTR(\2, \1)", s, flags=re.S)                                  # V140's suffix match
    s = _QUALIFY_RE.sub(lambda m: (f"SELECT {m['cols']}\n{m['ind']}FROM (SELECT *, {m['win']} AS RN_Q "
                                   f"FROM TASK_HISTORY WHERE {m['where']}) WHERE RN_Q = 1"), s)
    code = "".join(t for k, t in _lex(s) if k == "code")
    assert "::" not in code and "QUALIFY" not in code and "POSITION(" not in code and "COUNT_IF" not in code, code
    return s


# ---- shims ---------------------------------------------------------------------------------------------------
def _dec(x) -> Decimal:
    return Decimal(repr(float(x))) if isinstance(x, float) else Decimal(str(x))


def _hround(x, n, mode):
    assert mode == "HALF_TO_EVEN", mode
    if x is None:
        return None
    return float(_dec(x).quantize(Decimal(1).scaleb(-int(n)), ROUND_HALF_EVEN))


def _to_varchar(x, fmt=None):
    if x is None:
        return None
    if fmt is not None:
        assert fmt == "FM90.0", fmt
        return f"{_dec(x).quantize(Decimal('0.1'), ROUND_HALF_EVEN)}"
    return str(x)


def _ts(x) -> datetime:
    x = str(x)
    return datetime.strptime(x, _FMT) if len(x) > 10 else datetime.strptime(x, "%Y-%m-%d")


def _dateadd(unit, n, x):
    if x is None:
        return None
    step = {"day": timedelta(days=1), "hour": timedelta(hours=1)}[str(unit).lower()]
    return (_ts(x) + step * int(n)).strftime(_FMT)


def _datediff(unit, a, b):
    assert unit == "millisecond", unit
    return None if a is None or b is None else int((_ts(b) - _ts(a)).total_seconds() * 1000)


def _split_part(s, sep, k):
    if s is None:
        return None
    parts = str(s).split(sep)
    k = int(k)
    return parts[k - 1] if 1 <= k <= len(parts) else ""


def _regexp_replace(s, pat, rep):
    assert pat == "[[:space:]]", pat
    return None if s is None else re.sub(r"\s", rep, s)


class _Median:
    def __init__(self) -> None:
        self.v: list[float] = []

    def step(self, x) -> None:
        if x is not None:
            self.v.append(float(x))

    def finalize(self):
        return statistics.median(self.v) if self.v else None


class _Pct:
    def __init__(self) -> None:
        self.v: list[float] = []
        self.p = 0.5

    def step(self, x, p) -> None:
        self.p = float(p)
        if x is not None:
            self.v.append(float(x))

    def finalize(self):
        if not self.v:
            return None
        v = sorted(self.v)
        return v[min(len(v) - 1, round(self.p * (len(v) - 1)))]


def _company_for_database(scope: dict[str, str]):
    """V044:36-49 -- a COMPANY_SCOPE DATABASE row, then TRXS_ -> Trexis, then ALFA% / ADMIN -> ALFA, else UNKNOWN."""
    def fn(db):
        d = str(db or "").upper()
        if d in scope:
            return scope[d]
        if d.startswith("TRXS_"):
            return "Trexis"
        return "ALFA" if d.startswith("ALFA") or d == "ADMIN" else "UNKNOWN"
    return fn


_SCHEMAS = {
    "OBJECT_CHANGE_REGISTRY": "CHANGE_ID TEXT PRIMARY KEY, OBJECT_TYPE TEXT, DATABASE_NAME TEXT, SCHEMA_NAME TEXT, "
                              "OBJECT_NAME TEXT, COMPANY TEXT NOT NULL, CHANGE_SEEN_AT TEXT, BASELINE_FROM TEXT, "
                              "BASELINE_CALLS INT, BASELINE_FAILS INT, BASELINE_MEDIAN_MS REAL, BASELINE_P95_MS REAL, "
                              "BASELINE_CREDITS_PER_CALL REAL, AFTER_CALLS INT, TRACKING_UNTIL TEXT",
    "QUERY_HISTORY": "QUERY_ID TEXT, START_TIME TEXT, QUERY_TYPE TEXT, QUERY_TEXT TEXT, EXECUTION_STATUS TEXT, "
                     "TOTAL_ELAPSED_TIME REAL",
    "TASK_HISTORY": "QUERY_ID TEXT, DATABASE_NAME TEXT, SCHEMA_NAME TEXT, NAME TEXT, SCHEDULED_TIME TEXT, "
                    "QUERY_START_TIME TEXT, COMPLETED_TIME TEXT, STATE TEXT",
    "QUERY_ATTRIBUTION_HISTORY": "QUERY_ID TEXT, ROOT_QUERY_ID TEXT, START_TIME TEXT, "
                                 "CREDITS_ATTRIBUTED_COMPUTE REAL, CREDITS_USED_QUERY_ACCELERATION REAL",
    "ALERT_EVENTS": "EVENT_ID TEXT PRIMARY KEY, RULE_ID TEXT, COMPANY TEXT NOT NULL, STATUS TEXT, DEDUPE_KEY TEXT",
    "INCIDENT_MEMBERS": "INCIDENT_ID TEXT, MEMBER_KIND TEXT, REF_ID TEXT",
    "SCHEMA_VERSION": "VERSION INT",
    "PROCEDURES": "PROCEDURE_CATALOG TEXT, PROCEDURE_SCHEMA TEXT, PROCEDURE_NAME TEXT, DELETED TEXT",
    "VD": "BASELINE_P95_MS REAL, AFTER_P95_MS REAL, BASELINE_P95_S REAL, AFTER_P95_S REAL, "
          "BASELINE_QUEUED_MIN_PER_DAY REAL, AFTER_QUEUED_MIN_PER_DAY REAL",
}


def _db(scope: dict[str, str] | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    fns = {
        "NOW_TS": (0, lambda: _NOW.strftime(_FMT)), "TODAY_D": (0, lambda: _NOW.date().isoformat()),
        "TO_DATE_S": (1, lambda x: None if x is None else str(x)[:10]),
        "HROUND": (3, _hround), "TO_VARCHAR": (-1, _to_varchar), "IFF": (3, lambda c, a, b: a if c else b),
        "FLOOR": (1, lambda x: None if x is None else float(_dec(x).to_integral_value(rounding="ROUND_FLOOR"))),
        "MOD": (2, lambda a, b: None if a is None else float(_dec(a) % _dec(b))),
        "DATEADD": (3, _dateadd), "DATEDIFF": (3, _datediff),
        "GREATEST": (-1, lambda *a: None if any(v is None for v in a) else max(a)),
        "SPLIT_PART": (3, _split_part), "REGEXP_REPLACE": (3, _regexp_replace),
        "ENDSWITH": (2, lambda a, b: None if a is None or b is None else int(str(a).endswith(str(b)))),
        "COMPANY_FOR_DATABASE": (1, _company_for_database(scope or {})), "CHR": (1, lambda n: chr(int(n))),
    }
    for name, (narg, fn) in fns.items():
        con.create_function(name, narg, fn)
    con.create_aggregate("MEDIAN", 1, _Median)
    con.create_aggregate("APPROX_PERCENTILE", 2, _Pct)
    for table, cols in _SCHEMAS.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
    return con


def _t(days: float = 0, hours: float = 0, minutes: float = 0) -> str:
    return (_NOW + timedelta(days=days, hours=hours, minutes=minutes)).strftime(_FMT)


# ============================================================================================================
# R1-124: the HD template, executed
# ============================================================================================================
_CASE_RE = re.compile(r"CASE WHEN \((?P<s>[^\n]*?)\) IS NULL THEN '(?P<nul>[^']*)'\n.*?\n\s*END", re.S)


def _cases(body: str) -> list[re.Match]:
    vd = _between(body, "           VERDICT_DETAIL =\n", "     WHERE CURRENT_DATE() <= TRACKING_UNTIL;")
    return list(_CASE_RE.finditer(vd))


def test_harness_finds_every_hd_block():
    assert [m["s"] for m in _cases(_CI)] == ["BASELINE_P95_MS / 1000", "AFTER_P95_MS / 1000"]
    assert [m["s"] for m in _cases(_WH)] == ["BASELINE_P95_S", "AFTER_P95_S",
                                             "COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60",
                                             "COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60"]
    assert [m["nul"] for m in _cases(_WH)] == ["?", "?", "0s", "0s"]


def _seconds(expr: str, row: dict) -> float | None:
    col = re.search(r"(BASELINE|AFTER)_\w+", expr).group(0)
    v = row[col]
    if "QUEUED" in col:
        return (0.0 if v is None else v) * 60
    if v is None:
        return None
    return v / 1000 if col.endswith("_MS") else v


_MS_GRID = [0, 0.4, 0.5, 1, 500, 999.6, 1000, 5040, 9960, 10000, 45000, 59400, 60000, 94500, 95500, 119600,
            1_800_000, 3_599_600, 3_600_000, 3_630_000, 3_690_000, 8_700_000, 86_400_000, 90_061_000, None]
_S_GRID = [0.0, 0.5, 1.0, 5.0, 9.9, 10.0, 45.0, 59.4, 94.5, 95.5, 1800.0, 3599.6, 3630.0, 8700.0, 86400.0, None]
_MIN_GRID = [0.0, 0.01, 0.15, 1.5, 145.0, 200.0, 1439.99, None]


@pytest.mark.parametrize("which", ["object scan (V140 -> V172)", "warehouse scan (V109 -> V172)"])
def test_hd_template_renders_like_humanize_duration(which):
    from app.logic.formulas import humanize_duration
    con = _db()
    rows = [{"BASELINE_P95_MS": _MS_GRID[k % len(_MS_GRID)], "AFTER_P95_MS": _MS_GRID[(k + 7) % len(_MS_GRID)],
             "BASELINE_P95_S": _S_GRID[k % len(_S_GRID)], "AFTER_P95_S": _S_GRID[(k + 3) % len(_S_GRID)],
             "BASELINE_QUEUED_MIN_PER_DAY": _MIN_GRID[k % len(_MIN_GRID)],
             "AFTER_QUEUED_MIN_PER_DAY": _MIN_GRID[(k + 2) % len(_MIN_GRID)]}
            for k in range(max(len(_MS_GRID), len(_S_GRID), len(_MIN_GRID)))]
    cols = list(rows[0])
    con.executemany(f"INSERT INTO VD ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in rows])
    body = _CI if which.startswith("object") else _WH
    checked = 0
    for m in _cases(body):
        got = [r[0] for r in con.execute(f"SELECT {_to_sqlite(m.group(0))} FROM VD ORDER BY ROWID")]
        for row, text in zip(rows, got, strict=True):
            s = _seconds(m["s"], row)
            want = m["nul"] if s is None else humanize_duration(s, "s")
            assert text == want, (m["s"], row, text, want)
            checked += 1
    assert checked >= 50


# ============================================================================================================
# R2-025: the TASK count legs collapse retries; R2-022: settled per-run AFTER credits/call
# ============================================================================================================
def _task(con, name: str, sched: str, attempts: list[tuple[str, str, str, str]]) -> None:
    """attempts: (QUERY_ID, start, completed, state) of one scheduled run."""
    for qid, start, done, state in attempts:
        con.execute("INSERT INTO TASK_HISTORY VALUES (?, 'DB1', 'S', ?, ?, ?, ?, ?)",
                    (qid, name, sched, start, done, state))


def _registry(con, cid: str, otype: str, obj: str, seen: str, **cols) -> None:
    db, sch, _n = obj.split(".")
    until = (datetime.strptime(seen[:10], "%Y-%m-%d") + timedelta(days=14)).date().isoformat()
    base = {"CHANGE_ID": cid, "OBJECT_TYPE": otype, "DATABASE_NAME": db, "SCHEMA_NAME": sch, "OBJECT_NAME": obj,
            "COMPANY": "ALFA", "CHANGE_SEEN_AT": seen, "TRACKING_UNTIL": until}
    base.update(cols)
    con.execute(f"INSERT INTO OBJECT_CHANGE_REGISTRY ({', '.join(base)}) VALUES ({', '.join('?' * len(base))})",
                tuple(base.values()))


def _retry_fixture(con, name: str, first_sched: datetime, runs: int, retried: set[int]) -> None:
    for k in range(runs):
        sched = first_sched + timedelta(days=k)
        s = sched.strftime(_FMT)
        if k in retried:      # attempt 1 fails, the auto-retry succeeds: ONE scheduled run, which succeeded
            _task(con, name, s, [(f"{name}{k}a", (sched + timedelta(seconds=5)).strftime(_FMT),
                                  (sched + timedelta(minutes=10)).strftime(_FMT), "FAILED"),
                                 (f"{name}{k}b", (sched + timedelta(minutes=11)).strftime(_FMT),
                                  (sched + timedelta(minutes=20)).strftime(_FMT), "SUCCEEDED")])
        else:
            _task(con, name, s, [(f"{name}{k}", (sched + timedelta(seconds=5)).strftime(_FMT),
                                  (sched + timedelta(minutes=9)).strftime(_FMT), "SUCCEEDED")])


def _count_leg(body: str, n: int) -> str:
    """The n-th TASK count leg's derived ``s`` (0 = step-3 baseline, 1 = step-4 after)."""
    return _between(body, "          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,\n                 COUNT_IF(h.STATE", "      ) s\n", n)


def test_task_count_legs_count_scheduled_runs_not_attempts():
    """7 of 14 runs retried once (the first attempt failed, the retry succeeded): V140 read 21 runs and 7 failures
    (REGRESSED; CRITICAL from a 50% fail rate) though every scheduled run succeeded; V172 reads 14 runs, 0 failed."""
    con = _db()
    seen = _t(days=-6)
    _registry(con, "T1", "TASK", "DB1.S.TASK_A", seen)
    _retry_fixture(con, "TASK_A", _NOW - timedelta(days=20, hours=-1), 14, {0, 2, 4, 6, 8, 10, 12})   # before
    _retry_fixture(con, "TASK_A", _NOW - timedelta(days=5, hours=6), 4, {1})                           # after
    binds = {"trk_lo": f"'{seen}'"}
    base_new = con.execute(_to_sqlite(_count_leg(_CI, 0), binds)).fetchall()
    base_old = con.execute(_to_sqlite(_count_leg(_CI0, 0), binds)).fetchall()
    after_new = con.execute(_to_sqlite(_count_leg(_CI, 1), binds)).fetchall()
    after_old = con.execute(_to_sqlite(_count_leg(_CI0, 1), binds)).fetchall()
    assert [r[:3] for r in base_old] == [("T1", 21, 7)] and [r[:3] for r in base_new] == [("T1", 14, 0)]
    assert [r[:3] for r in after_old] == [("T1", 5, 1)] and [r[:3] for r in after_new] == [("T1", 4, 0)]
    # p95 is a terminal attempt's runtime (the 9-minute retry), never the failed first attempt's 9m55s
    assert base_new[0][4] == 540_000 and base_old[0][4] == 595_000


def _after_credits(body: str) -> str:
    return _between(body, "              SELECT x.CHANGE_ID, SUM(COALESCE(a.CR, 0))", "          ) s\n")


def test_after_credits_per_call_counts_settled_runs_per_scheduled_run():
    con = _db()
    seen = _t(days=-6)
    _registry(con, "T1", "TASK", "DB1.S.TASK_A", seen)
    _registry(con, "P1", "PROCEDURE", "DB1.S.SP_LOAD", seen)
    _registry(con, "P2", "PROCEDURE", "DB1.S.SP_IDLE", seen)
    # TASK_A after the change: a retried run (2 attempts), two clean runs, and one 6h-old (unsettled) run
    _task(con, "TASK_A", _t(days=-5), [("h1a", _t(days=-5, minutes=1), _t(days=-5, minutes=9), "FAILED"),
                                       ("h1b", _t(days=-5, minutes=10), _t(days=-5, minutes=19), "SUCCEEDED")])
    _task(con, "TASK_A", _t(days=-4), [("h2", _t(days=-4, minutes=1), _t(days=-4, minutes=9), "SUCCEEDED")])
    _task(con, "TASK_A", _t(days=-3), [("h3", _t(days=-3, minutes=1), _t(days=-3, minutes=9), "SUCCEEDED")])
    _task(con, "TASK_A", _t(hours=-6), [("h4", _t(hours=-6, minutes=1), _t(hours=-6, minutes=9), "SUCCEEDED")])
    calls = [("q1", _t(days=-5, hours=2), "CALL DB1.S.SP_LOAD()"),        # attributed through two children
             ("q2", _t(days=-4, hours=2), "call sp_load ()"),               # settled, NOT attributed: adds 0
             ("q3", _t(days=-3, hours=2), "CALL\tDB1.S.SP_LOAD(1)"),         # a tab: still SP_LOAD
             ("q4", _t(days=-3, hours=3), "CALL DB1.S.RUN_SP_LOAD()"),      # R2-021: not SP_LOAD
             ("q5", _t(hours=-2), "CALL DB1.S.SP_LOAD()"),                  # 2h old: not settled
             ("q6", _t(days=-2), "CALL DB1.S.SP_LOAD_V2()"),                # a longer name
             ("q7", _t(days=-2), "CALL SP_IDLE()")]                         # settled, never attributed
    for qid, start, text in calls:
        con.execute("INSERT INTO QUERY_HISTORY VALUES (?, ?, 'CALL', ?, 'SUCCESS', 1000)", (qid, start, text))
    qah = [("h1a", None, 1.0), ("h1b", None, 1.0), ("h2", None, 1.0), ("h3", None, 1.0), ("h4", None, 2.0),
           ("c1", "q1", 0.3), ("c2", "q1", 0.2), ("q3", None, 0.5), ("q4", None, 9.0), ("q5", None, 7.0),
           ("q6", None, 5.0)]
    for qid, root, cr in qah:
        con.execute("INSERT INTO QUERY_ATTRIBUTION_HISTORY VALUES (?, ?, ?, ?, NULL)", (qid, root, _t(days=-1), cr))
    binds = {"trk_lo": f"'{seen}'"}
    new = dict(con.execute(_to_sqlite(_after_credits(_CI), binds)).fetchall())
    assert set(new) == {"T1", "P1"}                         # P2: no settled run attributed -> no value (HAVING)
    assert new["T1"] == pytest.approx(4.0 / 3)              # 4 attempt credits over 3 settled scheduled runs
    assert new["P1"] == pytest.approx(1.0 / 3)              # (0.5 + 0 + 0.5) over 3 settled anchored calls
    # V140: attributed credits over its own population -- the unsettled run, RUN_SP_LOAD and SP_LOAD_V2 included
    old = dict(con.execute(_to_sqlite(_between(_CI0, "              SELECT x.CHANGE_ID, SUM(a.CR) AS TOTAL_CR",
                                               "          ) s\n", 1), binds)).fetchall())
    assert old["T1"] == pytest.approx(6.0) and old["P1"] == pytest.approx(0.5 + 0.5 + 9.0 + 7.0)   # TOTAL_CR


# ============================================================================================================
# The repairs R1-R4
# ============================================================================================================
def _repairs() -> list[str]:
    start = _MIG.index("-- R1 (R2-023)")
    end = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    stmts = [s.strip() + ";" for s in _MIG[start:end].split(";\n") if s.strip()]
    assert len(stmts) == 5
    return stmts


def _repair_fixture(con) -> None:
    seen = _t(days=-11)
    _registry(con, "r1", "PROCEDURE", "MAPPED_DB.S.SP_X", "2026-09-20 10:00:00")
    _registry(con, "r2", "PROCEDURE", "SHARED_DB.S.SP_Y", "2026-09-21 10:00:00")
    _registry(con, "r3", "TASK", "TRXS_DW.S.T1", seen, COMPANY="Trexis")
    frozen = {"BASELINE_FROM": _t(days=-25), "BASELINE_CALLS": 30, "BASELINE_FAILS": 1, "BASELINE_MEDIAN_MS": 5.0,
              "BASELINE_P95_MS": 9.0, "BASELINE_CREDITS_PER_CALL": 0.2}
    _registry(con, "r4", "PROCEDURE", "ALFA_EDW.S.SP_LOAD", seen, **frozen)        # RUN_SP_LOAD exists
    _registry(con, "r5", "PROCEDURE", "ALFA_EDW.S.SP_SOLO", seen, **frozen)        # only its own name matches
    _registry(con, "r6", "PROCEDURE", "ALFA_EDW.S.SP_OLD", "2026-08-01 10:00:00", **frozen)   # tracking ended
    _registry(con, "r7", "TASK", "DB1.S.TASK_A", _NOW.strftime(_FMT)[:10] + " 00:00:00",
              BASELINE_FROM=_t(days=-14), BASELINE_CALLS=20, BASELINE_FAILS=7, BASELINE_MEDIAN_MS=1.0,
              BASELINE_P95_MS=600_000, BASELINE_CREDITS_PER_CALL=0.1)
    _retry_fixture(con, "TASK_A", _NOW - timedelta(days=13, hours=7), 13, {0, 2, 4, 6, 8, 10, 12})
    for cat, sch, name, deleted in (("ALFA_EDW", "S", "SP_LOAD", None), ("OPS", "S", "RUN_SP_LOAD", "2026-09-01"),
                                    ("ALFA_EDW", "S", "SP_SOLO", None), ("X", "Y", "OLD_SP_OLD", None)):
        con.execute("INSERT INTO PROCEDURES VALUES (?, ?, ?, ?)", (cat, sch, name, deleted))
    events = [("e1", "PERF_CHANGE_REGRESSION", "ALFA", "OPEN", "PERF_CHANGE_REGRESSION|MAPPED_DB.S.SP_X|2026-09-20"),
              ("e2", "PERF_CHANGE_REGRESSION", "ALFA", "RESOLVED", "PERF_CHANGE_REGRESSION|MAPPED_DB.S.SP_X|2026-09-19"),
              ("e3", "PERF_CHANGE_REGRESSION", "ALFA", "ACK", "PERF_CHANGE_REGRESSION|MAPPED_DB.S.SP_Z|2026-09-22"),
              ("e4", "PIPE_DT_FAILURES", "ALFA", "OPEN", "PIPE_DT_FAILURES|MAPPED_DB.SCH.DT1|2026-09-30"),
              ("e5", "DQ_BREACH", "ALFA", "SNOOZED", "DQ_BREACH|SHARED_DB.S.T|2026-09-29"),
              ("e6", "PIPE_VOLUME_DROP", "Trexis", "ACK", "PIPE_VOLUME_DROP|TRXS_EDW_PRD.S.T|2026-09-30"),
              ("e7", "DQ_SCHEMA_DRIFT", "ALFA", "OPEN", "DQ_SCHEMA_DRIFT|ALFA_EDW.S.T|2026-09-30"),
              ("e8", "COST_ANOMALY_SWEEP", "ALFA", "OPEN", "COST_ANOMALY_SWEEP|WAREHOUSE SHARED_DB|2026-09-30"),
              ("e9", "PERF_CHANGE_REGRESSION", "ALFA", "OPEN", "PERF_CHANGE_REGRESSION|SHARED_DB.S.SP_Y|2026-09-22"),
              ("e10", "PIPE_DT_FAILURES", "ALFA", "OPEN", "PIPE_DT_FAILURES|MAPPED_DB.SCH.DT2|2026-09-29")]
    con.executemany("INSERT INTO ALERT_EVENTS VALUES (?, ?, ?, ?, ?)", events)
    con.executemany("INSERT INTO INCIDENT_MEMBERS VALUES (?, 'ALERT', ?)", [("INC1", "e3"), ("INC2", "e10")])
    con.execute("INSERT INTO SCHEMA_VERSION VALUES (171)")


def _run_repairs(con) -> None:
    for stmt in _repairs():
        con.execute(_to_sqlite(stmt))


def _snapshot(con) -> tuple:
    return (con.execute("SELECT * FROM OBJECT_CHANGE_REGISTRY ORDER BY CHANGE_ID").fetchall(),
            con.execute("SELECT * FROM ALERT_EVENTS ORDER BY EVENT_ID").fetchall())


def test_repairs_restamp_company_from_the_database_in_the_fqn():
    con = _db({"MAPPED_DB": "Trexis"})
    _repair_fixture(con)
    _run_repairs(con)
    reg = dict(con.execute("SELECT CHANGE_ID, COMPANY FROM OBJECT_CHANGE_REGISTRY").fetchall())
    assert reg == {"r1": "Trexis", "r2": "UNKNOWN", "r3": "Trexis", "r4": "ALFA", "r5": "ALFA", "r6": "ALFA",
                   "r7": "UNKNOWN"}                                    # DB1 is unmapped: UNKNOWN, never ALFA
    ev = dict(con.execute("SELECT EVENT_ID, COMPANY FROM ALERT_EVENTS").fetchall())
    assert ev["e1"] == "Trexis"            # the mapped database, via the re-stamped registry row
    assert ev["e2"] == "ALFA"              # RESOLVED history stays as raised
    assert ev["e3"] == "ALFA"              # incident-linked: keeps its incident's company
    assert ev["e4"] == "Trexis"            # part 2 'MAPPED_DB.SCH.DT1' split to MAPPED_DB (whole -> UNKNOWN)
    assert ev["e5"] == "UNKNOWN" and ev["e6"] == "Trexis" and ev["e7"] == "ALFA"
    assert ev["e8"] == "ALFA"              # another rule: untouched
    assert ev["e9"] == "UNKNOWN"           # no registry row for that key: the FQN fallback
    assert ev["e10"] == "ALFA"             # incident-linked
    assert _company_for_database({"MAPPED_DB": "Trexis"})("MAPPED_DB.SCH.DT1") == "UNKNOWN"   # why part 2 is split


def test_repairs_null_suffix_collided_baselines_once_and_refreeze_task_baselines():
    con = _db({"MAPPED_DB": "Trexis"})
    _repair_fixture(con)
    _run_repairs(con)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT CHANGE_ID, BASELINE_FROM, BASELINE_CALLS, BASELINE_FAILS, BASELINE_P95_MS, BASELINE_CREDITS_PER_CALL "
        "FROM OBJECT_CHANGE_REGISTRY")}
    assert rows["r4"] == (None, None, None, None, None)                  # RUN_SP_LOAD (even deleted) collides
    assert rows["r5"][1] == 30 and rows["r6"][1] == 30                     # no collision / tracking ended
    assert rows["r7"][0] == _t(days=-14)                                   # the freeze date stays
    assert rows["r7"][1:4] == (13, 0, 540_000)                             # terminal attempts: 13 runs, 0 failed
    assert rows["r7"][4] == pytest.approx(0.1 * 20 / 13)                   # the all-attempt numerator, rescaled
    # the version row lands, the 06:50 scan re-freezes r4 -- and a re-run of the whole file changes nothing
    con.execute("INSERT INTO SCHEMA_VERSION VALUES (172)")
    con.execute("UPDATE OBJECT_CHANGE_REGISTRY SET BASELINE_FROM = ?, BASELINE_CALLS = 12, BASELINE_FAILS = 0 "
                "WHERE CHANGE_ID = 'r4'", (_t(days=-25),))
    before = _snapshot(con)
    _run_repairs(con)
    assert _snapshot(con) == before


def test_repairs_rerun_before_the_version_row_is_harmless():
    """A file that stopped after the repairs re-runs them: the re-stamps and the TASK re-freeze are fixed points."""
    con = _db({"MAPPED_DB": "Trexis"})
    _repair_fixture(con)
    _run_repairs(con)
    first = _snapshot(con)
    _run_repairs(con)
    assert _snapshot(con) == first


def test_translation_fails_closed():
    with pytest.raises(AssertionError):
        _to_sqlite("SELECT :unknown_bind")
    assert _to_sqlite("UPDATE X t\n   SET A = 1") == "UPDATE X AS t\n   SET A = 1"
    assert _to_sqlite("SELECT ROUND(MOD(a, 60), 0, 'HALF_TO_EVEN')::INT") == \
        "SELECT CAST(HROUND(MOD(a, 60), 0, 'HALF_TO_EVEN') AS INTEGER)"
    assert _hround(2.5, 0, "HALF_TO_EVEN") == 2.0 and _hround(3.5, 0, "HALF_TO_EVEN") == 4.0
    assert date.fromisoformat(_NOW.date().isoformat()) == _NOW.date()
