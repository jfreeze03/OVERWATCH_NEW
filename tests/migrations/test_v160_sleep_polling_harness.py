"""Executed harness for V160's SP_SCAN_SLEEP_POLLING -- the COST_SLEEP_POLLING chronic sleep-polling alert.

The proc's OWN text (read from the V160 migration, never a hand-written twin) runs in an in-memory sqlite. The
body is split string- and comment-aware (the sleep pattern literal holds '--' and '//'); each SQL statement is
translated minimally -- FQNs dropped, the Central clock pinned, Snowflake string escapes decoded (so the
pattern's \\n is the LF Snowflake sees), LEAST / GREATEST null-safe, IFF, COUNT_IF, MAX_BY, LISTAGG ... WITHIN
GROUP, REGEXP_INSTR / REGEXP_SUBSTR with POSIX [[:space:]], DATEADD / DAYOFWEEKISO / TO_DATE on ISO strings and
COMPANY_FOR_WAREHOUSE shimmed -- and fails closed on anything it does not know. The scripting layer (DECLARE
defaults, SELECT ... INTO, IF ... THEN ... END IF, :=, SQLROWCOUNT, RETURN) is interpreted from the body too,
so gate -> ready / defer -> snapshot -> raise -> supersede -> clear -> receipt is the proc's own IF logic.
Clocks are Central wall-clock 'YYYY-MM-DD HH:MM:SS' (the account TIMEZONE is Central); a DAY is 'YYYY-MM-DD'.
VARCHAR widths of ALERT_EVENTS (V004) and SLEEP_POLLING_WEEKLY (the migration's DDL) are CHECK constraints, so
an over-long key, TITLE or census value fails here as it would in Snowflake.

Covers: executed parity with the v4.595 Spend panel (mart_sql.cloud_svc_billed_families through the same
harness, then cs_driver.billed_family_view) on the owner's 2026-09-26 DIAG shape and on a binding day; the
poller grain and the exclusions; the raise / hold / supersede / clear matrix; the weekly gate, the readiness
deferral and the receipt-last retry contract.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.data import mart_sql
from app.logic import cs_driver, navigate, system_wait

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V160__sleep_polling_alert.sql").read_text(encoding="utf-8")
_RULE = "COST_SLEEP_POLLING"
_RATE = 3.68
_TS = "%Y-%m-%d %H:%M:%S"
_NOW = datetime(2026, 9, 28, 7, 5)                   # Monday 07:05 Central: the first daily scan after apply
_TODAY = _NOW.date()
_WEEK = "2026-09-28"
_KEY_RE = re.compile(r"^COST_SLEEP_POLLING\|[^|]+\|[^|]+\|(MED|HIGH)\|\d{4}-\d{2}-\d{2}$")


def _day(k: int) -> str:
    return (_TODAY + timedelta(days=k)).isoformat()


def _ts(d: datetime) -> str:
    return d.strftime(_TS)


# the 7 complete metering days when the newest FACT_METERING_DAILY row is today's (in progress at 06:45)
_WIN = tuple(_day(k) for k in range(-7, 0))          # 2026-09-21 .. 2026-09-27


# ============================================================================================================
# Snowflake-aware scanning (literals first: the sleep pattern literal holds -- and //)
# ============================================================================================================
def _skip_literal(sql: str, i: int, *, backslash: bool) -> int:
    j, n = i + 1, len(sql)
    while j < n:
        if backslash and sql[j] == "\\":
            j += 2
            continue
        if sql[j] == "'":
            if sql[j + 1:j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    raise AssertionError(f"unterminated literal: {sql[i:i + 60]!r}")


def _lex(sql: str, *, backslash: bool = True) -> list[tuple[str, str]]:
    """-> [(kind, text)]: 'code', 'str' (RAW literal text between the quotes) or 'comment'."""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two in ("--", "//") or two == "/*" or sql[i] == "'":
            if buf:
                out.append(("code", "".join(buf)))
                buf = []
            if sql[i] == "'":
                j = _skip_literal(sql, i, backslash=backslash)
                out.append(("str", sql[i + 1:j - 1]))
            elif two == "/*":
                j = sql.index("*/", i + 2) + 2
                out.append(("comment", sql[i:j]))
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


_SF_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0",
               "\\": "\\", "'": "'", '"': '"'}


def _sf_decode(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "'":
            out.append("'")
            i += 2
        elif raw[i] == "\\":
            assert raw[i + 1] in _SF_ESCAPES, f"unhandled Snowflake escape in {raw!r}"
            out.append(_SF_ESCAPES[raw[i + 1]])
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _close(sql: str, i: int, *, backslash: bool) -> int:
    assert sql[i] == "(", sql[i:i + 40]
    depth = 0
    while i < len(sql):
        if sql[i] == "'":
            i = _skip_literal(sql, i, backslash=backslash)
            continue
        depth += (sql[i] == "(") - (sql[i] == ")")
        if depth == 0:
            return i
        i += 1
    raise AssertionError("unbalanced parentheses")


def _split_top(text: str) -> list[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        if text[i] == "'":
            i = _skip_literal(text, i, backslash=False)
            continue
        depth += (text[i] == "(") - (text[i] == ")")
        if text[i] == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    parts.append(text[start:].strip())
    return parts


def _statements(block: str) -> list[tuple[str, str]]:
    """``block`` -> [(label, statement)]: split at top-level ';' outside literals and comments; comments
    dropped (literals kept raw); label = the latest ``-- [name]`` comment before the statement began."""
    stmts: list[tuple[str, str]] = []
    cur: list[str] = []
    depth, label, cur_label = 0, "", None
    for kind, text in _lex(block):
        if kind == "comment":
            m = re.match(r"--\s*\[(\w+)\]", text)
            if m:
                label = m.group(1)
                if "".join(cur).strip().upper() in ("BEGIN", "DECLARE"):   # the block keyword is not the statement
                    cur_label = label
            continue
        if kind == "str":
            cur_label = label if cur_label is None else cur_label
            cur.append(f"'{text}'")
            continue
        for ch in text:
            if ch == ";" and depth == 0:
                if "".join(cur).strip():
                    stmts.append((cur_label or label, "".join(cur).strip()))
                cur, cur_label = [], None
                continue
            if cur_label is None and not ch.isspace():
                cur_label = label
            depth += (ch == "(") - (ch == ")")
            cur.append(ch)
    assert not "".join(cur).strip() and depth == 0, "trailing text after the last ';'"
    return stmts


# ============================================================================================================
# The proc, parsed from the migration
# ============================================================================================================
def _proc_body(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}(")
    o = text.index("$$", s) + 2
    return text[o:text.index("$$;", o)]


_BODY = _proc_body(_MIG, "SP_SCAN_SLEEP_POLLING")
_BINDABLE = re.compile(r":?\b[A-Za-z_]\w*\b")


def _ops(label: str, stmt: str) -> list[tuple[str, str, object]]:
    """One scripting statement -> interpreter ops. Anything else (ELSE, LOOP, LET, EXCEPTION ...) fails."""
    up = stmt.upper()
    if re.match(r"IF\s*\(", up):
        o = stmt.index("(")
        c = _close(stmt, o, backslash=True)
        rest = stmt[c + 1:].lstrip()
        assert rest.upper().startswith("THEN"), stmt[:80]
        inner = rest[4:].strip()
        return [("IF", label, stmt[o + 1:c])] + (_ops(label, inner) if inner else [])
    if re.fullmatch(r"END\s+IF", up):
        return [("ENDIF", label, "")]
    if up.startswith("RETURN "):
        return [("RETURN", label, stmt[7:])]
    m = re.fullmatch(r"(\w+)\s*:=\s*(.+)", stmt, re.S)
    if m:
        return [("ASSIGN", label, (m.group(1).upper(), m.group(2)))]
    m = re.fullmatch(r"SELECT\s+(.*?)\s+INTO\s+((?::\w+\s*,\s*)*:\w+)\s+(FROM\b.*)", stmt, re.S | re.I)
    if m:
        names = tuple(n.strip()[1:].upper() for n in m.group(2).split(","))
        return [("INTO", label, (f"SELECT {m.group(1)}\n{m.group(3)}", names))]
    if re.match(r"(INSERT|DELETE|UPDATE)\b", up):
        return [("SQL", label, stmt)]
    raise AssertionError(f"scripting construct the harness does not interpret: {stmt[:80]!r}")


def _program(body: str) -> tuple[dict[str, str | None], list[tuple[str, str, object]]]:
    decls: dict[str, str | None] = {}
    prog: list[tuple[str, str, object]] = []
    phase = "decl"
    for label, stmt in _statements(body):
        s = stmt
        if phase == "decl":
            if s.upper().startswith("DECLARE"):
                s = s[len("DECLARE"):].strip()
            if s.upper().startswith("BEGIN"):
                phase, s = "body", s[len("BEGIN"):].strip()
            else:
                m = re.fullmatch(r"(\w+)\s+\w+(?:\s+DEFAULT\s+(.+))?", s, re.S | re.I)
                assert m, s
                decls[m.group(1).upper()] = m.group(2)
                continue
        if s.upper() == "END":
            phase = "done"
            continue
        assert phase == "body", s[:80]
        prog.extend(_ops(label, s))
    assert phase == "done"
    return decls, prog


_DECLS, _PROGRAM = _program(_BODY)


# ============================================================================================================
# Snowflake -> sqlite
# ============================================================================================================
def _lit(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | float):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _listagg(s: str) -> str:
    """LISTAGG(v, sep) WITHIN GROUP (ORDER BY k1 [DESC], ...) -> LISTAGG_ORD(v, sep, 'dirs', k1, ...)."""
    while (i := s.find("LISTAGG(")) >= 0:
        o = i + len("LISTAGG")
        c = _close(s, o, backslash=False)
        args = _split_top(s[o + 1:c])
        m = re.match(r"\s*WITHIN GROUP\s*\(\s*ORDER BY\s+", s[c + 1:], re.I)
        assert m and len(args) == 2, s[i:i + 120]
        go = s.index("(", c + 1)
        gc = _close(s, go, backslash=False)
        dirs, keys = "", []
        for k in _split_top(s[c + 1 + m.end():gc]):
            km = re.fullmatch(r"(.*?)\s+(ASC|DESC)", k, re.S | re.I)
            dirs += km.group(2)[0].upper() if km else "A"
            keys.append(km.group(1) if km else k)
        s = s[:i] + f"LISTAGG_ORD({args[0]}, {args[1]}, '{dirs}', {', '.join(keys)})" + s[gc + 1:]
    return s


def _derived_to_cte(sql: str) -> str:
    """``SELECT b.* FROM (<inner>) b (cols) WHERE ...`` -> CTE ``b(cols)`` (sqlite has no derived column list)."""
    m = re.search(r"\n(?P<sel>[ ]*SELECT b\.RULE_ID[^\n]*)\n[ ]*FROM \(\n(?P<inner>.*)\n[ ]*\) b "
                  r"\((?P<cols>[^)]*)\)\n", sql, re.S)
    if not m:
        return sql
    return (sql[:m.start()].rstrip() + f",\nb({m['cols']}) AS (\n{m['inner']}\n)\n{m['sel']}\nFROM b\n"
            + sql[m.end():])


def _to_sqlite(sql: str, env: dict | None = None, *, bare: bool = False) -> str:
    s = sql.replace("DBA_MAINT_DB.OVERWATCH.", "")
    s = s.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE", "TODAY_D()")
    s = s.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ", "NOW_TS()")
    s = s.replace("CURRENT_TIMESTAMP()", "NOW_TS()").replace("CURRENT_ROLE()", "'TEST_ROLE'")
    s = re.sub(r"(ROUND\([^()']*\))::INT\b", r"CAST(\1 AS INTEGER)", s)
    env = env or {}

    def bind(m: re.Match) -> str:
        tok = m.group(0)
        if tok.startswith(":"):
            assert tok[1:].upper() in env, f"unbound bind {tok}"
            return _lit(env[tok[1:].upper()])
        return _lit(env[tok.upper()]) if bare and tok.upper() in env else tok

    out = []
    for kind, text in _lex(s):
        assert kind != "comment", text
        if kind == "str":
            out.append("'" + _sf_decode(text).replace("'", "''") + "'")
            continue
        code = _BINDABLE.sub(bind, text)
        assert "::" not in code and "CONVERT_TIMEZONE" not in code and "QUALIFY" not in code, code
        assert re.search(r":\w", code) is None, code
        out.append(code)
    s = "".join(out)
    s = _derived_to_cte(_listagg(s))
    s = re.sub(r"\bUPDATE (\w+) (\w+)\n", r"UPDATE \1 AS \2\n", s)
    assert "WITHIN GROUP" not in s.upper()
    return s


# ---- shims ---------------------------------------------------------------------------------------------------
def _nullsafe(fn):
    return lambda *a: None if any(v is None for v in a) else fn(*a)


def _dateadd(unit, n, x):
    if unit is None or n is None or x is None:
        return None
    n = int(n) if float(n).is_integer() else float(n)
    s = str(x)
    if len(s) == 10:
        assert str(unit).lower() == "day", unit
        return (date.fromisoformat(s) + timedelta(days=n)).isoformat()
    return _ts(datetime.strptime(s, _TS) + timedelta(**{str(unit).lower() + "s": n}))


def _posix(p: str) -> str:
    q = p.replace("[[:space:]]", r"\s")
    assert "[[:" not in q, p
    return q


def _regexp_instr(s, p):
    if s is None or p is None:
        return None
    m = re.search(_posix(p), s)
    return m.start() + 1 if m else 0


def _regexp_substr(s, p):
    if s is None or p is None:
        return None
    m = re.search(_posix(p), s)
    return m.group(0) if m else None


def _to_varchar(x):
    if x is None or isinstance(x, str):
        return x
    return format(x, ".15g") if isinstance(x, float) else str(x)


def _try_to_double(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _company_for_warehouse(wh):
    w = str(wh or "").upper()
    return "Trexis" if w.startswith("WH_TRXS_") else "ALFA" if w.startswith("WH_ALFA_") else None


class _CountIf:
    def __init__(self) -> None:
        self.n = 0

    def step(self, v) -> None:
        self.n += 1 if v else 0

    def finalize(self) -> int:
        return self.n


class _MaxBy:
    def __init__(self) -> None:
        self.best = None
        self.v = None

    def step(self, v, k) -> None:
        if k is not None and (self.best is None or k > self.best):
            self.best, self.v = k, v

    def finalize(self):
        return self.v


class _ListAggOrd:
    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.sep, self.dirs = "", ""

    def step(self, v, sep, dirs, *keys) -> None:
        self.sep, self.dirs = sep, dirs
        self.rows.append((v, keys))

    def finalize(self):
        rows = list(self.rows)
        for idx in reversed(range(len(self.dirs))):
            rows.sort(key=lambda r, i=idx: (r[1][i] is None, r[1][i]), reverse=self.dirs[idx] == "D")
        vals = [v for v, _ in rows if v is not None]
        return self.sep.join(vals) if rows else None


# ---- schemas -------------------------------------------------------------------------------------------------
_SCHEMAS = {
    "FACT_METERING_DAILY": "DAY TEXT, SERVICE_TYPE TEXT, CREDITS_COMPUTE REAL, CREDITS_CLOUD_SVCS REAL, "
                           "CREDITS_ADJUSTMENT REAL",
    "MART_CLOUD_SVC_DAILY": "DAY TEXT, COMPANY TEXT, WAREHOUSE_NAME TEXT NOT NULL, USER_NAME TEXT NOT NULL, "
                            "ROLE_NAME TEXT NOT NULL, QUERY_TYPE TEXT NOT NULL, QUERY_PARAMETERIZED_HASH TEXT, "
                            "SAMPLE_TEXT TEXT, RUNS INT, CS_CREDITS REAL, EXEC_SEC_SUM REAL, COMPILE_SEC_SUM REAL",
    "MART_QUERY_FAMILY_DAILY": "DAY TEXT, COMPANY TEXT, QUERY_HASH TEXT, TOTAL_ELAPSED_SEC REAL, "
                               "TOTAL_EXEC_SEC REAL, RUNS INT",
    "FACT_APP_COST_DAILY": "DAY TEXT, COMPANY TEXT, USER_NAME TEXT, APPLICATION TEXT, QUERIES INT",
    "SETTINGS": "KEY TEXT, VALUE TEXT",
    "ALERT_CONFIG": "RULE_ID TEXT, SEVERITY TEXT, ENABLED INT, THRESHOLD_NUM REAL, CLEAR_THRESHOLD_NUM REAL, "
                    "AUTO_CLEAR_ENABLED INT",
    # V004 widths as CHECKs: a key / TITLE / DETAIL too long for Snowflake fails here too
    "ALERT_EVENTS": "EVENT_ID INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "RULE_ID TEXT NOT NULL CHECK (length(RULE_ID) <= 60), "
                    "COMPANY TEXT NOT NULL DEFAULT 'ALL' CHECK (length(COMPANY) <= 40), "
                    "SEVERITY TEXT NOT NULL CHECK (length(SEVERITY) <= 20), "
                    "TITLE TEXT NOT NULL CHECK (length(TITLE) <= 300), "
                    "DETAIL TEXT CHECK (length(DETAIL) <= 2000), METRIC_VALUE REAL, "
                    "DEDUPE_KEY TEXT CHECK (length(DEDUPE_KEY) <= 300), "
                    "STATUS TEXT NOT NULL DEFAULT 'OPEN', RAISED_AT TEXT, RESOLVED_AT TEXT, RESOLUTION_KIND TEXT",
    "APP_ERROR_LOG": "PAGE TEXT CHECK (length(PAGE) <= 80), ERROR_TYPE TEXT CHECK (length(ERROR_TYPE) <= 200), "
                     "ERROR_MESSAGE TEXT CHECK (length(ERROR_MESSAGE) <= 2000), "
                     "CONTEXT TEXT CHECK (length(CONTEXT) <= 2000), ROLE_NAME TEXT",
}


def _weekly_ddl() -> tuple[str, list[str]]:
    """SLEEP_POLLING_WEEKLY exactly as the migration declares it (VARCHAR(n) -> a length CHECK)."""
    i = _MIG.index("CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY (")
    ((_, ddl),) = _statements(_MIG[i:_MIG.index("\n);\n", i) + 3])
    cols = re.findall(r"^\s*(\w+)\s+(?:VARCHAR|NUMBER|DATE|FLOAT|TIMESTAMP_NTZ)\b", ddl, re.M)
    ddl = ddl.replace("CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.", "CREATE TABLE ")
    ddl = ddl.replace("DEFAULT CURRENT_TIMESTAMP()", "DEFAULT CURRENT_TIMESTAMP")
    ddl = re.sub(r"\b(\w+)(\s+)VARCHAR\((\d+)\)", r"\1\2VARCHAR(\3) CHECK (length(\1) <= \3)", ddl)
    return ddl, cols


_WEEKLY_DDL, _WEEKLY_COLS = _weekly_ddl()


# ============================================================================================================
# The database + the interpreter
# ============================================================================================================
class _Injected(RuntimeError):
    pass


@dataclass
class _Result:
    ret: str
    trace: list[tuple[str, str]]
    raised: list[dict] = field(default_factory=list)
    superseded: list[dict] = field(default_factory=list)
    cleared: list[dict] = field(default_factory=list)

    def dml(self) -> list[tuple[str, str]]:
        return [t for t in self.trace if t[1] in ("INSERT", "DELETE", "UPDATE")]


def _poller_of(key: str) -> str:
    return key[:-15] if key[-16:-10] == "|HIGH|" else key[:-14]


class _Db:
    def __init__(self, now: datetime = _NOW) -> None:
        self.now = now
        con = sqlite3.connect(":memory:", isolation_level=None)       # autocommit, like Snowflake DML
        con.execute("PRAGMA case_sensitive_like = ON")                 # Snowflake LIKE is case-sensitive
        pure = {
            "DATEADD": (3, _dateadd), "DAYOFWEEKISO": (1, _nullsafe(lambda d: date.fromisoformat(str(d)[:10])
                                                                    .isoweekday())),
            "TO_DATE": (1, _nullsafe(lambda x: str(x)[:10])), "TO_VARCHAR": (1, _to_varchar),
            "TRY_TO_DOUBLE": (1, _try_to_double), "LEFT": (2, _nullsafe(lambda s, n: str(s)[:int(n)])),
            "IFF": (3, lambda c, a, b: a if c else b),
            "LEAST": (-1, _nullsafe(lambda *a: min(a))), "GREATEST": (-1, _nullsafe(lambda *a: max(a))),
            "REGEXP_INSTR": (2, _regexp_instr), "REGEXP_SUBSTR": (2, _regexp_substr),
            "COMPANY_FOR_WAREHOUSE": (1, _company_for_warehouse),
        }
        for name, (narg, fn) in pure.items():
            con.create_function(name, narg, fn, deterministic=True)
        con.create_function("NOW_TS", 0, lambda: _ts(self.now))
        con.create_function("TODAY_D", 0, lambda: self.now.date().isoformat())
        con.create_aggregate("COUNT_IF", 1, _CountIf)
        con.create_aggregate("MAX_BY", 2, _MaxBy)
        con.create_aggregate("LISTAGG_ORD", -1, _ListAggOrd)
        for table, cols in _SCHEMAS.items():
            con.execute(f"CREATE TABLE {table} ({cols})")
        con.execute(_WEEKLY_DDL)
        con.execute("CREATE TRIGGER ae_raised_at AFTER INSERT ON ALERT_EVENTS WHEN NEW.RAISED_AT IS NULL "
                    "BEGIN UPDATE ALERT_EVENTS SET RAISED_AT = NOW_TS() WHERE EVENT_ID = NEW.EVENT_ID; END")
        con.execute("INSERT INTO SETTINGS VALUES ('CREDIT_PRICE_USD', '3.68')")
        self.con = con
        self.cfg()

    # ---- seeding -------------------------------------------------------------------------------------------
    def cfg(self, **over) -> None:
        row = {"RULE_ID": _RULE, "SEVERITY": "MEDIUM", "ENABLED": 1, "THRESHOLD_NUM": 25,
               "CLEAR_THRESHOLD_NUM": None, "AUTO_CLEAR_ENABLED": 1}
        row.update(over)
        self.con.execute("DELETE FROM ALERT_CONFIG WHERE RULE_ID = ?", (_RULE,))
        if row.pop("MISSING", False):
            return
        self.con.execute(f"INSERT INTO ALERT_CONFIG ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                         tuple(row.values()))

    def meter(self, day: str, cs: float = 31.0, adj: float = -15.0) -> None:
        self.con.execute("INSERT INTO FACT_METERING_DAILY VALUES (?, 'WAREHOUSE_METERING', 100.0, ?, ?)",
                         (day, cs, adj))

    def stmt(self, day: str, h: str, text: str, cs: float, runs: int = 100, *, wh: str, user: str,
             role: str = "R", qtype: str = "SELECT", compile_s: float = 0.02) -> None:
        self.con.execute("INSERT INTO MART_CLOUD_SVC_DAILY VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (day, _company_for_warehouse(wh) or "ALFA", wh, user, role, qtype, h, text, runs, cs,
                          0.0, compile_s * runs))

    def app(self, user: str, application: str, queries: int, days: tuple[str, ...] = _WIN) -> None:
        for d in days:
            self.con.execute("INSERT INTO FACT_APP_COST_DAILY VALUES (?, 'ALFA', ?, ?, ?)",
                             (d, user, application, queries))

    def event(self, key: str, *, status: str = "OPEN", raised: datetime | None = None,
              resolved: datetime | None = None, kind: str | None = None, severity: str = "MEDIUM") -> int:
        cur = self.con.execute(
            "INSERT INTO ALERT_EVENTS (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY, "
            "STATUS, RAISED_AT, RESOLVED_AT, RESOLUTION_KIND) VALUES (?, 'ALL', ?, 't', 'd', 1, ?, ?, ?, ?, ?)",
            (_RULE, severity, key, status,
             _ts(raised or min(_NOW - timedelta(days=7), (resolved or _NOW) - timedelta(hours=1))),
             _ts(resolved) if resolved else None, kind))
        return int(cur.lastrowid)

    # ---- reads ---------------------------------------------------------------------------------------------
    def rows(self, sql: str, *params) -> list[dict]:
        cur = self.con.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def weekly(self, week: str = _WEEK) -> dict[str, dict]:
        return {r["POLLER_KEY"]: r for r in self.rows(
            "SELECT * FROM SLEEP_POLLING_WEEKLY WHERE WEEK_START = ?", week)}

    def pollers(self, week: str = _WEEK) -> dict[str, dict]:
        return {k: r for k, r in self.weekly(week).items() if r["ROW_KIND"] == "POLLER"}

    def account(self, week: str = _WEEK) -> dict:
        (row,) = [r for r in self.weekly(week).values() if r["ROW_KIND"] == "ACCOUNT"]
        return row

    def events(self) -> dict[int, dict]:
        return {r["EVENT_ID"]: r for r in self.rows("SELECT * FROM ALERT_EVENTS")}

    def panel(self, days: int = 7, current: date = _TODAY) -> pd.DataFrame:
        """The v4.595 Spend panel's REAL SQL through the same translation (CURRENT_DATE pinned)."""
        sql = re.sub(r"\b[A-Z_]+\.[A-Z_]+\.((?:FACT|MART)_[A-Z_]+)", r"\1", mart_sql.cloud_svc_billed_families(days))
        sql = sql.replace("CURRENT_DATE()", f"'{current.isoformat()}'")
        cur = self.con.execute(_to_sqlite(sql))
        return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])

    # ---- the proc ------------------------------------------------------------------------------------------
    def _value(self, expr: str, env: dict, *, bare: bool):
        return self.con.execute("SELECT " + _to_sqlite(expr, env, bare=bare)).fetchone()[0]

    def run(self, force_run: object = False, *, fail_before: str | None = None) -> _Result:
        before = self.events()
        env: dict = {name: (self._value(d, {}, bare=False) if d is not None else None)
                     for name, d in _DECLS.items()}
        env["FORCE_RUN"], env["SQLROWCOUNT"] = force_run, None
        trace: list[tuple[str, str]] = []
        skip = 0
        ret = None
        for kind, label, arg in _PROGRAM:
            if skip:
                skip += (kind == "IF") - (kind == "ENDIF")
                continue
            if fail_before and label == fail_before and kind in ("SQL", "INTO"):
                raise _Injected(f"injected failure before [{label}]")
            if kind == "IF":
                probe = re.search(r"\bSELECT\b", str(arg), re.I) is not None
                if probe:
                    trace.append((label, "PROBE"))
                if not self._value(f"CASE WHEN ({arg}) THEN 1 ELSE 0 END", env, bare=not probe):
                    skip = 1
            elif kind == "ASSIGN":
                name, expr = arg
                env[name] = self._value(expr, env, bare=True)
            elif kind == "INTO":
                sql, names = arg
                trace.append((label, "SELECT"))
                env.update(zip(names, self.con.execute(_to_sqlite(sql, env)).fetchone(), strict=True))
            elif kind == "SQL":
                trace.append((label, str(arg).split(None, 1)[0].upper()))
                env["SQLROWCOUNT"] = self.con.execute(_to_sqlite(str(arg), env)).rowcount
            elif kind == "RETURN":
                ret = self._value(str(arg), env, bare=True)
                break
        assert ret is not None, "the proc fell off its end without a RETURN"
        return self._result(ret, trace, before)

    def _result(self, ret: str, trace: list, before: dict[int, dict]) -> _Result:
        after = self.events()
        now = _ts(self.now)
        res = _Result(ret, trace)
        res.raised = [e for i, e in after.items() if i not in before]
        for i, e in after.items():
            if i in before and before[i]["STATUS"] != "RESOLVED" and e["STATUS"] == "RESOLVED":
                assert e["RESOLVED_AT"] == now
                (res.superseded if e["RESOLUTION_KIND"] == "SUPERSEDED" else res.cleared).append(e)
        # invariants of every run: a new event is born OPEN and stays so, and a raise and a clear never both
        # fire for the same poller in one run
        assert all(e["STATUS"] == "OPEN" for e in res.raised), res.raised
        assert all(e["RESOLUTION_KIND"] == "CONDITION_ENDED" for e in res.cleared)
        both = {_poller_of(e["DEDUPE_KEY"]) for e in res.raised} & {_poller_of(e["DEDUPE_KEY"]) for e in res.cleared}
        assert not both, f"raised AND cleared in one run: {both}"
        return res


# ============================================================================================================
# Fixtures
# ============================================================================================================
_CTM = {"wh": "WH_ALFA_TRANSFORM_PRD", "user": "CTM_SVC", "role": "CTM_ROLE", "qtype": "SELECT"}
_TRXS = {"wh": "WH_TRXS_TRANSFORM", "user": "SYSTEM", "role": "TRXS_TASK_OWNER", "qtype": "CALL"}
_PK_CTM = f"{_RULE}|WH_ALFA_TRANSFORM_PRD|CTM_SVC|"
_PK_TRXS = f"{_RULE}|WH_TRXS_TRANSFORM|SYSTEM:TRXS_TASK_OWNER|"
_WAITS = (("H_W30", "CALL SYSTEM$WAIT(30)", 2.3, 500), ("H_W60", "CALL SYSTEM$WAIT(60)", 1.5, 158),
          ("H_W1200", "CALL SYSTEM$WAIT(1200)", 0.3, 2))


def _owner(db: _Db, *, billed: dict[str, float] | None = None, lag: int = 0) -> None:
    """The owner's 2026-09-26 DIAG shape: Control-M 'select system$wait(10)' 28.8 CS/week (4.2 + 6 x 4.1) and
    the TRXS tasks' CALL SYSTEM$WAIT(30/60/1200) 28.5 CS/week (3.9 + 6 x 4.1), one owner role; the account
    billed 16 CS a day (never binding) unless ``billed`` overrides a day; plus an ordinary family, the newest
    (in-progress) metering row and statements on every day after the window (all unpriced). ``lag`` days of
    metering publish lag shift the window back: newest = today - lag."""
    billed = billed or {}
    win = tuple(_day(k - lag) for k in range(-7, 0))
    for i, d in enumerate(win):
        b = billed.get(d, 16.0)
        db.meter(d, 31.0, b - 31.0)
        db.stmt(d, "H_CTM", "select system$wait(10)", 4.2 if i == 0 else 4.1, 2667, **_CTM)
        for h, text, cs, runs in _WAITS:
            db.stmt(d, h, text, 0.1 if (i == 0 and h == "H_W1200") else cs, runs, **_TRXS)
        db.stmt(d, "H_BIG", "select * from sales.orders", 3.0, 40, wh="WH_ALFA_BI", user="ANALYST",
                compile_s=0.9)
    db.meter(_day(-lag), 11.0, -11.0)                     # the newest row: the UTC day still in progress
    for k in range(-lag, 1):                              # ... and every statement day after the window
        db.stmt(_day(k), "H_CTM", "select system$wait(10)", 3.9, 2540, **_CTM)
        db.stmt(_day(k), "H_W30", "CALL SYSTEM$WAIT(30)", 2.3, 500, **_TRXS)
    db.app("CTM_SVC", "Control-M", 500, days=win)
    db.app("CTM_SVC", "JDBC", 100, days=win)


def _flat(db: _Db, h: str, text: str, per_day: float, *, days: tuple[str, ...] = _WIN, **who) -> None:
    for d in days:
        db.stmt(d, h, text, per_day, 10, **who)


def _metered(db: _Db, days: tuple[str, ...] = _WIN, billed: float = 16.0) -> None:
    """Complete metering on ``days`` + today's in-progress row, and an ordinary (non-sleep) family on every day
    so the readiness check sees statement rows on all 7."""
    for d in days:
        db.meter(d, 31.0, billed - 31.0)
        db.stmt(d, "H_FILL", "select 1", 0.01, 5, wh="WH_ALFA_FILL", user="FILL")
    db.meter(_day(0), 11.0, -11.0)


@pytest.fixture()
def owner() -> _Db:
    db = _Db()
    _owner(db)
    return db


# ============================================================================================================
# Executed parity with the Spend panel
# ============================================================================================================
def test_owner_fixture_two_pollers_priced_like_the_panel(owner):
    res = owner.run()
    assert res.ret == (f"sleep polling scan week of {_WEEK}: 2 poller(s) over 2026-09-21..2026-09-27; "
                       "raised 2, superseded 0, cleared 0")
    pollers = owner.pollers()
    assert set(pollers) == {_PK_CTM, _PK_TRXS}
    ctm, trxs = pollers[_PK_CTM], pollers[_PK_TRXS]
    assert (ctm["USD_WEEK"], trxs["USD_WEEK"]) == (105.98, 104.88)                  # 28.8 / 28.5 CS x 3.68
    assert ctm["BILLED_CS_CREDITS"] == pytest.approx(28.8) and trxs["BILLED_CS_CREDITS"] == pytest.approx(28.5)
    assert (ctm["ACTIVE_DAYS"], ctm["FAMILIES"], ctm["RUNS"], ctm["LAST_ACTIVE_DAY"]) == (7, 1, 7 * 2667, _WIN[-1])
    assert (trxs["ACTIVE_DAYS"], trxs["FAMILIES"], trxs["RUNS"]) == (7, 3, 7 * 660)
    assert (ctm["TASK_ROLE"], trxs["TASK_ROLE"]) == ("", "TRXS_TASK_OWNER")
    assert (ctm["USER_TOP_APP"], ctm["OWNER_HINT"]) == ("Control-M", "Control-M · CTM_SVC")
    assert trxs["OWNER_HINT"] == "Task owner (role TRXS_TASK_OWNER)"
    assert trxs["CALLS"].startswith("CALL SYSTEM$WAIT(30) 16.1 cr; CALL SYSTEM$WAIT(60) 10.5 cr; ")  # largest first
    assert ctm["CALLS"] == "SELECT SYSTEM$WAIT(10) 28.8 cr"
    for r in (ctm, trxs):
        assert (r["WIN_START"], r["WIN_END"], r["ABOVE_ALLOWANCE_DAYS"], r["CREDIT_PRICE_USD"]) == (
            _WIN[0], _WIN[-1], 7, _RATE)
    acct = owner.account()
    assert acct["POLLER_KEY"] == f"{_RULE}|*|*|" and acct["COMPLETED_AT"] == _ts(_NOW)
    assert (acct["FAMILIES"], acct["POLLERS"], acct["ACTIVE_DAYS"], acct["RAISED"]) == (4, 2, 7, 2)
    # parity: sum of pollers == ACCOUNT == the panel's group total == billed_family_view's sleep_usd (no binding day)
    panel = owner.panel()
    _, summary = cs_driver.billed_family_view(panel, _RATE)
    total = ctm["BILLED_CS_CREDITS"] + trxs["BILLED_CS_CREDITS"]
    assert acct["BILLED_CS_CREDITS"] == pytest.approx(total)
    assert acct["BILLED_CS_CREDITS"] == pytest.approx(panel["SLEEP_BILLED_CS_CREDITS_ALL"].iloc[0], abs=1e-4)
    assert acct["BILLED_CS_CREDITS"] == pytest.approx(summary["sleep_usd"] / _RATE, abs=1e-4)
    assert acct["USD_WEEK"] == pytest.approx(summary["sleep_usd"], abs=0.006)
    assert acct["CS_CREDITS"] == pytest.approx(panel["SLEEP_CS_CREDITS_ALL"].iloc[0] - 3.9 - 2.3, abs=1e-4)
    assert acct["FAMILIES"] == summary["sleep_families"] == panel["SLEEP_FAMILIES_ALL"].iloc[0]
    assert panel["METERED_DAYS"].iloc[0] == 7 and panel["LAST_METERED_DAY"].iloc[0] == _WIN[-1]
    assert set(panel.loc[panel["SLEEP_FLAG"] == 1, "QUERY_PARAMETERIZED_HASH"]) == {"H_CTM", "H_W30", "H_W60",
                                                                                     "H_W1200"}


def test_owner_fixture_events_key_title_detail(owner):
    res = owner.run()
    by_key = {e["DEDUPE_KEY"]: e for e in res.raised}
    assert set(by_key) == {_PK_CTM + f"MED|{_WEEK}", _PK_TRXS + f"MED|{_WEEK}"}
    view, _ = cs_driver.billed_family_view(owner.panel(), _RATE)
    for pk, wh, company, usd, hash_ in ((_PK_CTM, "WH_ALFA_TRANSFORM_PRD", "ALFA", 105.98, "H_CTM"),
                                        (_PK_TRXS, "WH_TRXS_TRANSFORM", "Trexis", 104.88, "H_W30")):
        e = by_key[pk + f"MED|{_WEEK}"]
        assert _KEY_RE.match(e["DEDUPE_KEY"]) and len(e["DEDUPE_KEY"]) <= 256
        assert e["DEDUPE_KEY"].endswith(f"|{_WEEK}") and e["DEDUPE_KEY"][-11] == "|"   # V117's trailing date
        assert (e["RULE_ID"], e["COMPANY"], e["SEVERITY"], e["STATUS"]) == (_RULE, company, "MEDIUM", "OPEN")
        assert e["METRIC_VALUE"] == usd and e["RAISED_AT"] == _ts(_NOW)
        fam = view.loc[view["QUERY_PARAMETERIZED_HASH"] == hash_].iloc[0]
        assert e["TITLE"] == f"{wh} sleep polling ~${round(usd)}/week: {fam['OWNER_HINT']}"
        assert e["TITLE"].count("$") == 1 and len(e["TITLE"]) <= 300
        assert navigate._WH_RE.search(e["TITLE"].upper()).group(0) == wh
        assert len(e["DETAIL"]) <= 2000 and navigate._DB_RE.search(e["DETAIL"].upper()) is None
        assert f"Next step: {fam['NEXT_STEP']} Verify: " in e["DETAIL"]       # the panel row's own next step
        assert f"Owner: {fam['OWNER_HINT']}." in e["DETAIL"]
        assert f"~${round(usd)}/week, ~${round(usd * 52 / 12)}/month at $3.68/credit" in e["DETAIL"]
        assert "Sleep polling on 7 of the 7 complete days 2026-09-21 to 2026-09-27 (last on 2026-09-27)" in e["DETAIL"]
        assert navigate.investigation_target(_RULE, f"{e['TITLE']} {e['DETAIL']}")["filters"] == {
            "warehouse_contains": wh}


def test_one_day_publish_lag_prices_the_same_seven_days_as_the_panel_at_newest():
    """newest metering day = today-1 (in progress): the window is today-8 .. today-2; the newest day's and
    today's statements are never priced. The panel viewed with its 7-day window ending at the newest metering
    day covers the same 7 complete days."""
    db = _Db()
    _owner(db, lag=1)
    res = db.run()
    assert res.ret.startswith(f"sleep polling scan week of {_WEEK}: 2 poller(s) over 2026-09-20..2026-09-26;")
    ctm, trxs = db.pollers()[_PK_CTM], db.pollers()[_PK_TRXS]
    assert (ctm["USD_WEEK"], trxs["USD_WEEK"]) == (105.98, 104.88)
    assert (ctm["RUNS"], ctm["WIN_START"], ctm["WIN_END"], ctm["LAST_ACTIVE_DAY"]) == (
        7 * 2667, _day(-8), _day(-2), _day(-2))
    panel = db.panel(current=_TODAY - timedelta(days=1))
    assert db.account()["BILLED_CS_CREDITS"] == pytest.approx(panel["SLEEP_BILLED_CS_CREDITS_ALL"].iloc[0], abs=1e-4)
    assert panel["METERED_DAYS"].iloc[0] == 7 and panel["LAST_METERED_DAY"].iloc[0] == _day(-2)


def test_binding_day_caps_each_poller_as_a_group_and_the_account_like_the_panel():
    db = _Db()
    binding, under = _WIN[3], _WIN[5]
    _owner(db, billed={binding: 3.0, under: -2.0})     # 3 billed CS vs 8.2 of sleep; one day under the allowance
    db.run()
    pollers, acct = db.pollers(), db.account()
    cap = dict.fromkeys(_WIN, 16.0) | {binding: 3.0, under: 0.0}
    trxs_day = {d: (3.9 if i == 0 else 4.1) for i, d in enumerate(_WIN)}
    ctm_day = {d: (4.2 if i == 0 else 4.1) for i, d in enumerate(_WIN)}
    trxs_billed = sum(min(trxs_day[d], cap[d]) for d in _WIN)
    assert pollers[_PK_TRXS]["BILLED_CS_CREDITS"] == pytest.approx(trxs_billed)        # the poller is ONE group
    assert pollers[_PK_CTM]["BILLED_CS_CREDITS"] == pytest.approx(sum(min(ctm_day[d], cap[d]) for d in _WIN))
    panel = db.panel()
    fam_sum = panel.loc[panel["QUERY_PARAMETERIZED_HASH"].isin(["H_W30", "H_W60", "H_W1200"]),
                        "BILLED_CS_CREDITS"].sum()
    assert fam_sum - trxs_billed == pytest.approx(4.1 - 3.0)   # never the sum of per-family marginals
    group = sum(min(ctm_day[d] + trxs_day[d], cap[d]) for d in _WIN)
    assert acct["BILLED_CS_CREDITS"] == pytest.approx(group)
    assert acct["BILLED_CS_CREDITS"] == pytest.approx(panel["SLEEP_BILLED_CS_CREDITS_ALL"].iloc[0], abs=1e-4)
    assert sum(p["BILLED_CS_CREDITS"] for p in pollers.values()) > acct["BILLED_CS_CREDITS"] + 1.0
    assert acct["ABOVE_ALLOWANCE_DAYS"] == 6 and panel["UNDER_ALLOWANCE_DAYS"].iloc[0] == 1
    assert pollers[_PK_TRXS]["USD_WEEK"] == round(trxs_billed * _RATE, 2)


def test_poller_grain_exclusions_and_owner_hints():
    db = _Db()
    _metered(db)
    _flat(db, "H_W30", "CALL SYSTEM$WAIT(30)", 1.0, wh="WH_TRXS_TRANSFORM", user="SYSTEM", role="ROLE_A", qtype="CALL")
    _flat(db, "H_W30", "CALL SYSTEM$WAIT(30)", 0.5, wh="WH_TRXS_TRANSFORM", user="SYSTEM", role="ROLE_B", qtype="CALL")
    _flat(db, "H_CMT", "-- poll landing\nselect system$wait(5)", 0.4, wh="WH_ALFA_X", user="BOB")    # the \n escape
    _flat(db, "H_SL", "// poll\nCALL SYSTEM$WAIT(60)", 0.3, wh="WH_ALFA_X", user="BOB", qtype="CALL")
    _flat(db, "H_BLK", "/* ctm job */ select system$wait(10)", 0.2, wh="WH_ALFA_X", user="BOB")
    db.app("BOB", "(unknown)", 999)                                                # never the top app
    for h, text, qtype, hash_ in (
            ("M1", "select 1 -- system$wait(10)", "SELECT", "M1"),
            ("M2", "select query_text from qh where query_text ilike '%system$wait(%'", "SELECT", "M2"),
            ("M3", "CALL SYSTEM$WAIT(10)", "CREATE_TASK", "M3"),
            ("M4", "select system$wait(30); call dw.load_orders()", "MULTI_STATEMENT", "M4"),
            ("M5", "CALL SYSTEM$WAIT(30)", "CALL", "n/a"),
            ("M6", "SELECT SYSTEM$WAIT_FOR_SERVICES(60, 'svc')", "SELECT", "M6"),
            ("M7", "alter task t modify as call system$wait(1)", "ALTER_TASK", "M7")):
        _flat(db, hash_, text, 5.0, wh="WH_ALFA_NOISE", user=f"U_{h}", qtype=qtype)
    # the sleep is decided on the family's window-MIN sample, like the panel: '(' < '_' and 'B' < 'C'
    for i, d in enumerate(_WIN):
        db.stmt(d, "H_VMIN", "CALL SYSTEM$WAIT(5)" if i % 2 else "CALL SYSTEM$WAIT_FOR_SERVICES(5)", 5.0,
                wh="WH_ALFA_V", user="VMIN", qtype="CALL")
        db.stmt(d, "H_VMAX", "CALL SYSTEM$WAIT(5)" if i % 2 else "BEGIN CALL SYSTEM$WAIT(5); END", 5.0,
                wh="WH_ALFA_V", user="VMAX", qtype="CALL")
    db.stmt(_day(0), "H_NEW", "CALL SYSTEM$WAIT(5)", 9.0, wh="WH_ALFA_NEW", user="LATE", qtype="CALL")  # in progress
    db.stmt(_day(-8), "H_OLD", "CALL SYSTEM$WAIT(5)", 9.0, wh="WH_ALFA_OLD", user="GONE", qtype="CALL")  # pre-window
    db.run()
    pollers = db.pollers()
    assert set(pollers) == {f"{_RULE}|WH_TRXS_TRANSFORM|SYSTEM:ROLE_A|", f"{_RULE}|WH_TRXS_TRANSFORM|SYSTEM:ROLE_B|",
                            f"{_RULE}|WH_ALFA_X|BOB|", f"{_RULE}|WH_ALFA_V|VMIN|"}
    panel = db.panel()
    assert set(panel.loc[panel["SLEEP_FLAG"] == 1, "QUERY_PARAMETERIZED_HASH"]) == {
        "H_W30", "H_CMT", "H_SL", "H_BLK", "H_VMIN", "H_NEW"}             # H_NEW: today, unpriced in both
    bob = pollers[f"{_RULE}|WH_ALFA_X|BOB|"]
    assert bob["FAMILIES"] == 3 and bob["CS_CREDITS"] == pytest.approx(0.9 * 7)
    assert db.account()["FAMILIES"] == 5                  # H_W30 runs under two roles: one family, two pollers
    for p in pollers.values():
        row = {"USER_NAME": p["USER_NAME"], "ROLE_NAME": p["TASK_ROLE"], "USER_TOP_APP": p["USER_TOP_APP"]}
        assert p["OWNER_HINT"] == cs_driver.owner_hint(row)
    assert bob["OWNER_HINT"] == "User BOB" and bob["USER_TOP_APP"] is None
    assert pollers[f"{_RULE}|WH_TRXS_TRANSFORM|SYSTEM:ROLE_B|"]["OWNER_HINT"] == "Task owner (role ROLE_B)"


def test_owner_hint_user_with_app_matches_cs_driver(owner):
    owner.run()
    for p in owner.pollers().values():
        row = {"USER_NAME": p["USER_NAME"], "ROLE_NAME": p["TASK_ROLE"], "USER_TOP_APP": p["USER_TOP_APP"]}
        assert p["OWNER_HINT"] == cs_driver.owner_hint(row)


def test_sleep_predicate_matches_is_sleep_statement_on_the_shared_cases():
    """Every text of tests/test_system_wait (ALL_TEXTS) as its own family, under a SELECT and a DDL type: the
    census's sleepfam set is exactly system_wait.is_sleep_statement's."""
    from tests.test_system_wait import ALL_TEXTS

    db = _Db()
    _metered(db)
    expect = set()
    for i, text in enumerate(sorted(set(ALL_TEXTS))):
        for qtype in ("SELECT", "CREATE_TASK"):
            h = f"T{i:03d}{qtype[0]}"
            db.stmt(_WIN[0], h, text, 0.1, wh="WH_ALFA_T", user=h, qtype=qtype)
            if system_wait.is_sleep_statement(text, qtype):
                expect.add(h)
    db.run()
    got = {p["USER_NAME"] for p in db.pollers().values()}
    assert got == expect and len(expect) >= 15


# ============================================================================================================
# Raise
# ============================================================================================================
def _tiny(db: _Db) -> None:
    _metered(db)
    _flat(db, "H_T1", "select system$wait(1)", 0.03, wh="WH_ALFA_T", user="T1")    # 0.21 CS = $0.77/week
    _flat(db, "H_T2", "select system$wait(1)", 0.08, wh="WH_ALFA_T", user="T2")    # 0.56 CS = $2.06/week


def test_threshold_zero_is_floored_to_one():
    db = _Db()
    _tiny(db)
    db.cfg(THRESHOLD_NUM=0)
    res = db.run()
    assert [e["DEDUPE_KEY"] for e in res.raised] == [f"{_RULE}|WH_ALFA_T|T2|MED|{_WEEK}"]
    db2 = _Db()
    _tiny(db2)
    db2.cfg(THRESHOLD_NUM=None)                          # NULL -> the 25 USD default
    assert db2.run().raised == []


def test_threshold_is_inclusive_and_chronic_needs_five_of_seven_days(owner):
    owner.cfg(THRESHOLD_NUM=105.98)
    assert [e["DEDUPE_KEY"] for e in owner.run().raised] == [_PK_CTM + f"MED|{_WEEK}"]
    db = _Db()
    _metered(db)
    _flat(db, "H_A4", "CALL SYSTEM$WAIT(5)", 6.0, days=_WIN[3:], wh="WH_ALFA_A", user="FOUR", qtype="CALL")
    _flat(db, "H_A5", "CALL SYSTEM$WAIT(5)", 5.0, days=_WIN[2:], wh="WH_ALFA_A", user="FIVE", qtype="CALL")
    _flat(db, "H_F5", "CALL SYSTEM$WAIT(5)", 5.0, days=_WIN[:5], wh="WH_ALFA_A", user="FRONT", qtype="CALL")
    res = db.run()
    assert {e["DEDUPE_KEY"] for e in res.raised} == {f"{_RULE}|WH_ALFA_A|FIVE|MED|{_WEEK}",
                                                    f"{_RULE}|WH_ALFA_A|FRONT|MED|{_WEEK}"}
    assert db.pollers()[f"{_RULE}|WH_ALFA_A|FOUR|"]["ACTIVE_DAYS"] == 4


@pytest.mark.parametrize("base,thr,expect", [
    ("MEDIUM", 25, ("MEDIUM", "MED")), ("MEDIUM", 20, ("HIGH", "HIGH")), ("LOW", 25, ("LOW", "MED")),
    ("LOW", 20, ("HIGH", "HIGH")), ("HIGH", 25, ("HIGH", "MED")), ("HIGH", 20, ("HIGH", "HIGH")),
    ("CRITICAL", 25, ("CRITICAL", "MED")), ("CRITICAL", 20, ("CRITICAL", "HIGH")),
])
def test_band_at_five_x_and_severity_never_downgrades(owner, base, thr, expect):
    owner.cfg(SEVERITY=base, THRESHOLD_NUM=thr)          # 5 x 20 = 100 <= both pollers; 5 x 25 = 125 > both
    res = owner.run()
    assert len(res.raised) == 2
    for e in res.raised:
        assert (e["SEVERITY"], _KEY_RE.match(e["DEDUPE_KEY"]).group(1)) == expect


def _priced(price: str | None) -> _Db:
    db = _Db()
    db.con.execute("DELETE FROM SETTINGS")
    if price is not None:
        db.con.execute("INSERT INTO SETTINGS VALUES ('CREDIT_PRICE_USD', ?)", (price,))
    _metered(db)
    _flat(db, "H_AT1", "CALL SYSTEM$WAIT(5)", 1.0, days=_WIN[2:], wh="WH_ALFA_B", user="AT1X", qtype="CALL")
    _flat(db, "H_AT5", "CALL SYSTEM$WAIT(5)", 5.0, days=_WIN[2:], wh="WH_ALFA_B", user="AT5X", qtype="CALL")
    _flat(db, "H_UND", "CALL SYSTEM$WAIT(5)", 0.99, days=_WIN[2:], wh="WH_ALFA_B", user="UNDER", qtype="CALL")
    return db


def test_threshold_and_five_x_band_boundaries_are_inclusive():
    """At 5 USD/credit, 5 days x 1.0 CS = exactly 25 USD (= THR: raises MED) and 5 x 5.0 = exactly 125 USD
    (= 5 x THR: the HIGH band); 5 x 0.99 = 24.75 stays under."""
    db = _priced("5")
    res = db.run()
    got = {e["DEDUPE_KEY"].split("|")[2]: (_KEY_RE.match(e["DEDUPE_KEY"]).group(1), e["METRIC_VALUE"])
           for e in res.raised}
    assert got == {"AT1X": ("MED", 25.0), "AT5X": ("HIGH", 125.0)}
    assert db.pollers()[f"{_RULE}|WH_ALFA_B|UNDER|"]["USD_WEEK"] == 24.75


@pytest.mark.parametrize("price,rate", [("5", 5.0), ("4.10", 4.1), ("abc", 3.68), (None, 3.68)])
def test_credit_price_is_read_from_settings_with_a_safe_fallback(price, rate):
    db = _priced(price)
    db.run()
    p = db.pollers()[f"{_RULE}|WH_ALFA_B|AT5X|"]
    assert p["CREDIT_PRICE_USD"] == rate and db.account()["CREDIT_PRICE_USD"] == rate
    assert p["USD_WEEK"] == round(25.0 * rate, 2)
    detail = db.rows("SELECT DETAIL FROM ALERT_EVENTS WHERE DEDUPE_KEY LIKE ?", f"{_RULE}|WH_ALFA_B|AT5X|%")
    shown = re.search(r"/month at \$([0-9.]+)/credit\. ", detail[0]["DETAIL"])
    assert shown and float(shown.group(1)) == rate


def test_company_follows_the_warehouse():
    db = _Db()
    _metered(db)
    for wh in ("WH_ALFA_Q", "WH_TRXS_Q", "WH_OTHER", "NONE"):
        _flat(db, f"H_{wh}", "CALL SYSTEM$WAIT(5)", 5.0, wh=wh, user="U", qtype="CALL")
    raised = {e["DEDUPE_KEY"].split("|")[1]: e for e in db.run().raised}
    assert {k: e["COMPANY"] for k, e in raised.items()} == {"WH_ALFA_Q": "ALFA", "WH_TRXS_Q": "Trexis",
                                                            "WH_OTHER": "ALL", "NONE": "ALL"}
    assert raised["NONE"]["TITLE"].startswith("No warehouse sleep polling ~$")      # the mart's NULL warehouse
    assert navigate.investigation_target(_RULE, raised["NONE"]["TITLE"])["filters"] == {}


def test_key_segments_escape_pipes_and_stay_within_the_column():
    db = _Db()
    _metered(db)
    wh, user = "WH_ALFA_" + "W" * 150, "a|b" + "u" * 200
    _flat(db, "H_LONG", "CALL SYSTEM$WAIT(5)", 2.0, wh=wh, user=user, qtype="CALL")   # $51.52: MED
    (e,) = db.run().raised
    key = e["DEDUPE_KEY"]
    assert _KEY_RE.match(key) and key.count("|") == 4 and len(key) == 19 + 100 + 1 + 120 + 1 + 14
    assert key.split("|")[2].startswith("A/B") and len(db.pollers()) == 1
    assert _poller_of(key) in db.pollers()


# ============================================================================================================
# Hold (live episode, band-aware) and the exact-key guard
# ============================================================================================================
_LAST_WEEK = "2026-09-21"


@pytest.mark.parametrize("seed,thr,raises", [
    # live events hold their band
    ({"band": "MED", "status": "OPEN"}, 25, False),
    ({"band": "MED", "status": "OPEN"}, 20, True),                  # a live MED lets the HIGH through
    ({"band": "MED", "status": "SNOOZED"}, 25, False),
    ({"band": "HIGH", "status": "ACK"}, 25, False),
    ({"band": "HIGH", "status": "ACK"}, 20, False),                 # a live HIGH blocks both
    # dismissal mutes that band for 28 days
    ({"band": "MED", "status": "RESOLVED", "kind": "NOISE", "ago": 27}, 25, False),
    ({"band": "MED", "status": "RESOLVED", "kind": "NOISE", "ago": 27}, 20, True),
    ({"band": "HIGH", "status": "RESOLVED", "kind": "EXPECTED", "ago": 27}, 20, False),
    ({"band": "MED", "status": "RESOLVED", "kind": "NOISE", "ago": 29}, 25, True),
    ({"band": "HIGH", "status": "RESOLVED", "kind": "EXPECTED", "ago": 29}, 20, True),
    # ACTIONED (and a NULL kind) re-raises only on polling after the resolve day (last active 2026-09-27)
    ({"band": "MED", "status": "RESOLVED", "kind": "ACTIONED", "resolved": "2026-09-27 12:00:00"}, 25, False),
    ({"band": "MED", "status": "RESOLVED", "kind": "ACTIONED", "resolved": "2026-09-26 23:59:00"}, 25, True),
    ({"band": "MED", "status": "RESOLVED", "kind": None, "resolved": "2026-09-27 00:30:00"}, 20, False),
    ({"band": "MED", "status": "RESOLVED", "kind": None, "resolved": "2026-09-25 09:00:00"}, 25, True),
    # machine closes never hold
    ({"band": "MED", "status": "RESOLVED", "kind": "CONDITION_ENDED", "resolved": "2026-09-27 12:00:00"}, 25, True),
    ({"band": "HIGH", "status": "RESOLVED", "kind": "SUPERSEDED", "resolved": "2026-09-27 12:00:00"}, 20, True),
    ({"band": "MED", "status": "RESOLVED", "kind": "AUTO_CLEARED", "resolved": "2026-09-27 12:00:00"}, 25, True),
    ({"band": "MED", "status": "RESOLVED", "kind": "SNOOZE_SUPPRESSED", "resolved": "2026-09-27 12:00:00"}, 25, True),
])
def test_hold_matrix(owner, seed, thr, raises):
    owner.cfg(THRESHOLD_NUM=thr)
    if "ago" in seed:
        resolved = _NOW - timedelta(days=seed["ago"])
    else:
        resolved = datetime.strptime(seed["resolved"], _TS) if "resolved" in seed else None
    owner.event(_PK_CTM + f"{seed['band']}|{_LAST_WEEK}", status=seed["status"], resolved=resolved,
                kind=seed.get("kind"))
    res = owner.run()
    ctm = [e for e in res.raised if e["DEDUPE_KEY"].startswith(_PK_CTM)]
    band = "HIGH" if thr == 20 else "MED"
    assert [e["DEDUPE_KEY"] for e in ctm] == ([_PK_CTM + f"{band}|{_WEEK}"] if raises else [])
    assert [e["DEDUPE_KEY"] for e in res.raised if e["DEDUPE_KEY"].startswith(_PK_TRXS)] == [
        _PK_TRXS + f"{band}|{_WEEK}"]                    # the other poller is never held by this one's events


def test_same_week_exact_key_never_duplicates_on_force_run(owner):
    first = owner.run()
    assert len(first.raised) == 2
    owner.con.execute("UPDATE ALERT_EVENTS SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'CONDITION_ENDED', "
                      "RESOLVED_AT = ?", (_ts(_NOW),))      # a machine close: no hold, only the exact key blocks
    owner.now = _NOW + timedelta(hours=2)
    again = owner.run(True)
    assert again.raised == [] and len(owner.events()) == 2
    assert again.ret.endswith("raised 0, superseded 0, cleared 0")


# ============================================================================================================
# Supersede
# ============================================================================================================
@pytest.mark.parametrize("status", ["OPEN", "ACK", "SNOOZED"])
def test_live_high_supersedes_the_same_pollers_med_from_any_week(owner, status):
    owner.cfg(THRESHOLD_NUM=21.1)                        # 5x = 105.5: CTM (105.98) HIGH, TRXS (104.88) MED
    old = owner.event(_PK_CTM + "MED|2026-09-14", status=status, raised=_NOW - timedelta(days=14))
    trxs = owner.event(_PK_TRXS + f"MED|{_LAST_WEEK}", status="OPEN")
    done = owner.event(_PK_CTM + "MED|2026-09-07", status="RESOLVED", resolved=_NOW - timedelta(days=15),
                       kind="EXPECTED")
    res = owner.run()
    assert [e["DEDUPE_KEY"] for e in res.raised] == [_PK_CTM + f"HIGH|{_WEEK}"]
    ev = owner.events()
    assert (ev[old]["STATUS"], ev[old]["RESOLUTION_KIND"]) == ("RESOLVED", "SUPERSEDED")
    assert (ev[trxs]["STATUS"], ev[trxs]["RESOLUTION_KIND"]) == ("OPEN", None)      # another poller: untouched
    assert ev[done]["RESOLUTION_KIND"] == "EXPECTED"
    assert owner.account()["SUPERSEDED"] == 1 and res.ret.endswith("raised 1, superseded 1, cleared 0")


def test_supersede_probe_runs_every_evaluation_even_without_a_raise(owner):
    owner.event(_PK_CTM + f"HIGH|{_LAST_WEEK}", status="ACK")
    med = owner.event(_PK_CTM + "MED|2026-09-14", status="SNOOZED")
    res = owner.run()                                    # CTM's MED candidate is held by the live HIGH
    assert all(not e["DEDUPE_KEY"].startswith(_PK_CTM) for e in res.raised)
    assert owner.events()[med]["RESOLUTION_KIND"] == "SUPERSEDED"
    assert ("supersede", "PROBE") in res.trace and ("supersede", "UPDATE") in res.trace


def test_no_live_high_means_no_supersede_update(owner):
    owner.event(_PK_CTM + "MED|2026-09-14", status="OPEN")
    res = owner.run()
    assert ("supersede", "PROBE") in res.trace and ("supersede", "UPDATE") not in res.trace
    assert res.superseded == []


# ============================================================================================================
# Clear
# ============================================================================================================
def _clear_fixture(db: _Db) -> None:
    _owner(db)
    _flat(db, "H_LOW", "CALL SYSTEM$WAIT(5)", 0.4, wh="WH_ALFA_LOW", user="LOW", qtype="CALL")   # $10.30 < 12.5
    _flat(db, "H_MID", "CALL SYSTEM$WAIT(5)", 0.8, wh="WH_ALFA_MID", user="MID", qtype="CALL")   # $20.61
    _flat(db, "H_IDLE", "CALL SYSTEM$WAIT(5)", 10.0, days=_WIN[:3], wh="WH_ALFA_IDL", user="IDLE", qtype="CALL")
    _flat(db, "H_EDGE", "CALL SYSTEM$WAIT(5)", 10.0, days=_WIN[:4], wh="WH_ALFA_EDG", user="EDGE", qtype="CALL")


_OLD = _NOW - timedelta(days=7)


def _clear_events(db: _Db) -> dict[str, int]:
    keys = {
        "ctm": _PK_CTM + f"MED|{_LAST_WEEK}",                                  # still polling, above -> kept
        "absent": f"{_RULE}|WH_GONE|GHOST|MED|{_LAST_WEEK}",                   # not in the census -> cleared
        "low": f"{_RULE}|WH_ALFA_LOW|LOW|MED|{_LAST_WEEK}",                    # under the clear level -> cleared
        "mid": f"{_RULE}|WH_ALFA_MID|MID|MED|{_LAST_WEEK}",                    # 20.6 >= 12.5 -> kept
        "idle": f"{_RULE}|WH_ALFA_IDL|IDLE|HIGH|{_LAST_WEEK}",                 # idle on the last 4 days -> cleared
        "edge": f"{_RULE}|WH_ALFA_EDG|EDGE|MED|{_LAST_WEEK}",                  # last active = WIN_END - 3 -> kept
    }
    ids = {name: db.event(k, raised=_OLD) for name, k in keys.items()}
    ids["ack"] = db.event(f"{_RULE}|WH_GONE|ACKED|MED|{_LAST_WEEK}", status="ACK", raised=_OLD)
    ids["snoozed"] = db.event(f"{_RULE}|WH_GONE|SNZ|MED|{_LAST_WEEK}", status="SNOOZED", raised=_OLD)
    ids["young"] = db.event(f"{_RULE}|WH_GONE|YOUNG|MED|{_LAST_WEEK}", raised=_NOW - timedelta(minutes=59))
    ids["hour"] = db.event(f"{_RULE}|WH_GONE|HOUR|MED|{_LAST_WEEK}", raised=_NOW - timedelta(hours=1))
    return ids


def test_clear_matrix():
    db = _Db()
    _clear_fixture(db)
    ids = _clear_events(db)
    res = db.run()
    ev = db.events()
    closed = {n for n, i in ids.items() if ev[i]["STATUS"] == "RESOLVED"}
    assert closed == {"absent", "low", "idle", "hour"}
    assert all(ev[ids[n]]["RESOLUTION_KIND"] == "CONDITION_ENDED" for n in closed)
    assert (ev[ids["ack"]]["STATUS"], ev[ids["snoozed"]]["STATUS"]) == ("ACK", "SNOOZED")
    assert db.account()["CLEARED"] == 4 and res.ret.endswith("cleared 4")
    assert {_poller_of(e["DEDUPE_KEY"]) for e in res.raised} == {_PK_TRXS}   # CTM / MID / EDGE held by their OPEN


def test_clear_is_opt_in():
    db = _Db()
    _clear_fixture(db)
    ids = _clear_events(db)
    db.cfg(AUTO_CLEAR_ENABLED=0)
    res = db.run()
    assert res.cleared == [] and ("clear", "UPDATE") not in res.trace and ("clear", "PROBE") in res.trace
    assert all(db.events()[i]["STATUS"] != "RESOLVED" for i in ids.values())


@pytest.mark.parametrize("clear_num,closed", [
    (None, {"absent", "low", "idle", "hour"}),           # half the threshold: 12.5
    (1000, {"absent", "low", "idle", "hour", "mid"}),    # clamped to THR = 25: MID (20.6) now clears, CTM kept
    (5, {"absent", "idle", "hour"}),                     # LOW (10.3) is above 5: kept
])
def test_clear_level_defaults_to_half_and_is_clamped_to_the_threshold(clear_num, closed):
    db = _Db()
    _clear_fixture(db)
    ids = _clear_events(db)
    db.cfg(CLEAR_THRESHOLD_NUM=clear_num)
    db.run()
    ev = db.events()
    assert {n for n, i in ids.items() if ev[i]["STATUS"] == "RESOLVED"} == closed


def test_a_week_with_no_sleep_still_writes_the_receipt_and_ends_open_episodes():
    db = _Db()
    _metered(db)
    old = db.event(_PK_CTM + f"MED|{_LAST_WEEK}", raised=_OLD)
    res = db.run()
    assert res.ret == (f"sleep polling scan week of {_WEEK}: 0 poller(s) over 2026-09-21..2026-09-27; "
                       "raised 0, superseded 0, cleared 1")
    acct = db.account()
    assert (acct["FAMILIES"], acct["POLLERS"], acct["ACTIVE_DAYS"], acct["USD_WEEK"]) == (0, 0, 0, 0)
    assert acct["BILLED_CS_CREDITS"] is None and acct["COMPLETED_AT"] == _ts(_NOW) and db.pollers() == {}
    assert db.events()[old]["RESOLUTION_KIND"] == "CONDITION_ENDED"


def test_an_under_allowance_week_bills_nothing_raises_nothing_and_clears():
    """Accepted risk (spec 3.10): cloud services under the free allowance all week = $0 billed, so the
    chronic poller neither raises nor keeps its episode open (DETAIL says billed, not used)."""
    db = _Db()
    _owner(db, billed=dict.fromkeys(_WIN, -1.0))
    old = db.event(_PK_TRXS + f"MED|{_LAST_WEEK}", raised=_OLD)
    res = db.run()
    assert res.raised == [] and [e["EVENT_ID"] for e in res.cleared] == [old]
    p = db.pollers()[_PK_CTM]
    assert (p["USD_WEEK"], p["BILLED_CS_CREDITS"], p["ABOVE_ALLOWANCE_DAYS"]) == (0, 0, 0)
    assert p["CS_CREDITS"] == pytest.approx(28.8)


def test_raise_and_clear_never_both_fire_for_one_poller():
    """A front-loaded chronic poller (active on the window's first 5 days) raises and is not cleared; its own
    new event is under the 1h dwell anyway, and a stale OPEN event of the same poller is kept, not cleared."""
    db = _Db()
    _metered(db)
    _flat(db, "H_F5", "CALL SYSTEM$WAIT(5)", 10.0, days=_WIN[:5], wh="WH_ALFA_F", user="FRONT", qtype="CALL")
    stale = db.event(f"{_RULE}|WH_ALFA_F|FRONT|MED|2026-09-14", status="OPEN", raised=_NOW - timedelta(days=14))
    db.cfg(THRESHOLD_NUM=20)                             # 184 USD >= 5 x 20: a HIGH breaks through the live MED
    res = db.run()                                       # _Result asserts the no-raise-and-clear invariant
    assert [e["DEDUPE_KEY"] for e in res.raised] == [f"{_RULE}|WH_ALFA_F|FRONT|HIGH|{_WEEK}"]
    assert db.events()[stale]["RESOLUTION_KIND"] == "SUPERSEDED" and res.cleared == []
    db.now = _NOW + timedelta(hours=3)
    again = db.run(True)                                 # past the dwell: still active and above -> kept
    assert again.cleared == [] and again.raised == []


# ============================================================================================================
# Gate, readiness and the receipt
# ============================================================================================================
def _receipt(db: _Db, week: str, completed: str | None) -> None:
    db.con.execute("INSERT INTO SLEEP_POLLING_WEEKLY (WEEK_START, ROW_KIND, POLLER_KEY, COMPLETED_AT) "
                   "VALUES (?, 'ACCOUNT', ?, ?)", (week, f"{_RULE}|*|*|", completed))


def test_receipt_this_week_skips_with_one_read(owner):
    _receipt(owner, _WEEK, "2026-09-28 07:01:00")
    res = owner.run()
    assert res.ret == f"sleep polling scan skipped (week of {_WEEK} already evaluated)"
    assert res.trace == [("gate", "SELECT")] and owner.events() == {}
    none = owner.run(None)                               # COALESCE(NULL, FALSE): a NULL is not a force
    assert none.trace == [("gate", "SELECT")]


def test_receipt_of_last_week_or_an_uncompleted_row_is_due(owner):
    _receipt(owner, _LAST_WEEK, "2026-09-21 07:01:00")
    _receipt(owner, _WEEK, None)                         # a failed run's census: no receipt
    res = owner.run()
    assert res.ret.startswith(f"sleep polling scan week of {_WEEK}:") and len(res.raised) == 2
    assert len(owner.weekly(_LAST_WEEK)) == 1            # the DELETE is scoped to this week
    assert [t for t in res.trace if t[0] in ("snapshot", "receipt")] == [
        ("snapshot", "DELETE"), ("snapshot", "INSERT"), ("receipt", "UPDATE")]


def test_force_run_re_evaluates_a_completed_week(owner):
    owner.run()
    owner.now = _NOW + timedelta(days=1)
    assert owner.run().trace == [("gate", "SELECT")]      # Tuesday: the receipt stops the rest of the week's calls
    res = owner.run(True)
    assert res.ret.startswith(f"sleep polling scan week of {_WEEK}:") and res.raised == []
    assert len(owner.weekly()) == 3 and owner.account()["COMPLETED_AT"] == _ts(owner.now)
    owner.now = _NOW + timedelta(days=6, hours=16)        # Sunday 23:05 (ISO day 7): still the same week
    assert owner.run().trace == [("gate", "SELECT")]
    owner.now = _NOW + timedelta(days=7)                  # next Monday: a new ISO week is due again (and the
    nxt = owner.run()                                     # fixture's metering stops at 09-28, so it defers)
    assert nxt.ret == "sleep polling scan deferred (data not complete)"
    assert _log_contexts(owner) == ["rule COST_SLEEP_POLLING - week of 2026-10-05 not evaluated; the next daily "
                                    "scan retries"]


@pytest.mark.parametrize("over", [{"ENABLED": 0}, {"MISSING": True}])
def test_disabled_or_missing_rule_skips_even_when_forced(owner, over):
    owner.cfg(**over)
    for force in (False, True):
        res = owner.run(force)
        assert res.ret == f"sleep polling scan skipped ({_RULE} disabled or missing)"
        assert res.trace == [("gate", "SELECT")]


def _log_contexts(db: _Db) -> list[str]:
    return [r["CONTEXT"] for r in db.rows("SELECT CONTEXT FROM APP_ERROR_LOG")]


def _stale(newest_offset: int, *, drop_mart_day: str | None = None, drop_meter_day: str | None = None) -> _Db:
    db = _Db()
    newest = _TODAY + timedelta(days=newest_offset)
    days = tuple((newest + timedelta(days=k)).isoformat() for k in range(-7, 0))
    for d in days:
        if d != drop_meter_day:
            db.meter(d)
        if d != drop_mart_day:
            db.stmt(d, "H_CTM", "select system$wait(10)", 4.1, 2667, **_CTM)
    db.meter(newest.isoformat(), 11.0, -11.0)
    _receipt(db, _WEEK, None)                            # a leftover census row: a deferral must not DELETE it
    return db


@pytest.mark.parametrize("db_args,deferred,n_days", [
    ({"newest_offset": -4}, True, 6),                    # newest metering day = today-4 (09-17 < today-10)
    ({"newest_offset": -3}, True, 7),                    # complete, but the newest day is too old
    ({"newest_offset": -2}, False, 7),                   # the readiness floor
    ({"newest_offset": 0, "drop_mart_day": _WIN[4]}, True, 6),
    ({"newest_offset": 0, "drop_meter_day": _WIN[0]}, True, 6),
    ({"newest_offset": 0}, False, 7),
])
def test_readiness_defers_without_touching_the_census(db_args, deferred, n_days):
    db = _stale(**db_args)
    res = db.run()
    if deferred:
        newest = (_TODAY + timedelta(days=db_args["newest_offset"])).isoformat()
        assert res.ret == "sleep polling scan deferred (data not complete)"
        assert res.dml() == [("ready", "INSERT")]        # the error-log row only: no DELETE, no census, no receipt
        (log,) = db.rows("SELECT * FROM APP_ERROR_LOG")
        assert (log["PAGE"], log["ERROR_TYPE"], log["ROLE_NAME"]) == ("AlertScan", "sleep_polling_scan_deferred",
                                                                      "TEST_ROLE")
        assert log["ERROR_MESSAGE"] == (f"newest metering day {newest}; {n_days} of the 7 window days carry "
                                        "both metering and statement rows")
        assert log["CONTEXT"] == (f"rule {_RULE} - week of {_WEEK} not evaluated; the next daily scan retries")
        assert len(db.weekly()) == 1 and db.account()["COMPLETED_AT"] is None
    else:
        assert res.ret.startswith(f"sleep polling scan week of {_WEEK}: 1 poller(s)")
        assert db.rows("SELECT * FROM APP_ERROR_LOG") == [] and db.account()["COMPLETED_AT"] == _ts(_NOW)


def test_failure_before_the_receipt_leaves_the_week_due(owner):
    with pytest.raises(_Injected):
        owner.run(fail_before="receipt")
    assert owner.account()["COMPLETED_AT"] is None       # autocommitted census + events, no receipt
    assert len(owner.events()) == 2
    owner.now = _NOW + timedelta(days=1)                  # Tuesday's daily scan redoes the week
    res = owner.run()
    assert res.ret.startswith(f"sleep polling scan week of {_WEEK}:") and res.raised == []
    assert len(owner.events()) == 2 and owner.account()["COMPLETED_AT"] == _ts(owner.now)
    owner.now = _NOW + timedelta(days=2)
    assert owner.run().trace == [("gate", "SELECT")]


def test_harness_is_not_vacuous():
    """Guard the guard: the interpreter saw the proc's whole flow and the census table as the migration has it."""
    stmts = [(label, kind if kind == "INTO" else str(arg).split(None, 1)[0].upper())
             for kind, label, arg in _PROGRAM if kind in ("INTO", "SQL")]
    assert stmts == [("gate", "INTO"), ("ready", "INTO"), ("ready", "INSERT"), ("snapshot", "DELETE"),
                     ("snapshot", "INSERT"), ("raise", "INSERT"), ("supersede", "UPDATE"), ("clear", "UPDATE"),
                     ("receipt", "UPDATE")]                  # the receipt is the LAST statement
    probes = [label for kind, label, arg in _PROGRAM if kind == "IF" and "SELECT" in str(arg)]
    assert probes == ["supersede", "clear"]
    assert [k for k, _, _ in _PROGRAM].count("RETURN") == 4
    assert set(_DECLS) >= {"TODAY_CT", "WEEK_START", "FORCED", "CREDIT_PRICE", "NEWEST", "N_DAYS"}
    assert len(_WEEKLY_COLS) == 26 and "COMPLETED_AT" in _WEEKLY_COLS
