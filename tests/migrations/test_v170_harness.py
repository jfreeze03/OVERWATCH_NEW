"""Executed harness for V170 -- INCIDENT_PROPOSALS and both SP_INCIDENT_DECLARE overloads, run in sqlite.

Every statement comes from the migration's OWN text (never a hand-written twin), so a band, rule list, predicate,
column or verdict edit made in outputs/gen_v170.py -- which the byte-identity test follows -- still fails here.
V072's view and V131's proc run through the SAME translation, so each defect is shown before and fixed after.

Translation (minimal, fails closed): FQNs dropped; CURRENT_TIMESTAMP() / CURRENT_USER() / UUID_STRING() are fixed
shims (CURRENT_USER() is the app owner, as under owner's-rights SiS); SPLIT_PART / DATEADD / TRY_TO_DATE / IFF /
DECODE / MAX_BY / LEFT / DATE / ARRAY_SIZE(SPLIT()) are shimmed; any other function, a '::' cast or an unknown
statement raises. The proc's Snowflake Scripting is walked statement by statement in its own order (assignments
and IF conditions are evaluated by sqlite from the proc text; the transaction is a real sqlite transaction).
Clocks are Central wall-clock 'YYYY-MM-DD HH:MM:SS' (the account TIMEZONE). The V032 widths are CHECKs, and the
DECLARED_BY / LINKED_BY defaults are the app owner, exactly what CURRENT_USER() yields for the owner's-rights app.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from tests._source import read
from tests.migrations.test_v170_incident_declare_actor_and_proposals import EXPECTED

_MIG = read("snowflake/migrations/V170__incident_declare_actor_and_proposals.sql")
_V131 = read("snowflake/migrations/V131__incident_declare_atomic.sql")
_V072 = read("snowflake/migrations/V072__entity_aware_incident_proposals.sql")
_CREATE_PROC = "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE("
_CREATE_VIEW = "CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS AS\n"

_TS = "%Y-%m-%d %H:%M:%S"
_NOW = datetime(2026, 10, 1, 9, 30)
_OWNER = "APP_OWNER"           # what CURRENT_USER() returns inside the owner's-rights app and its EXECUTE AS OWNER proc
_UUIDS = iter(f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 10_000))


def _procs(text: str) -> list[str]:
    out, at = [], 0
    while (s := text.find(_CREATE_PROC, at)) >= 0:
        o = text.index("$$", s)
        e = text.index("$$;", o + 2) + 3
        out.append(text[s:e])
        at = e
    return out


def _view_body(text: str) -> str:
    s = text.index(_CREATE_VIEW) + len(_CREATE_VIEW)
    return text[s:text.index("\nFROM evidence;\n", s) + len("\nFROM evidence")]


(_P131,) = _procs(_V131)
_P4, _P5 = _procs(_MIG)
_VIEW170, _VIEW072 = _view_body(_MIG), _view_body(_V072)


# ---------------------------------------------------------------------------------------------------------------
# sqlite shims
# ---------------------------------------------------------------------------------------------------------------
def _ts(d: datetime) -> str:
    return d.strftime(_TS)


def _ago(hours: float) -> str:
    return _ts(_NOW - timedelta(hours=hours))


def _dateadd(unit: str, n: float, x: str | None) -> str | None:
    if x is None:
        return None
    if len(x) == 10:                                   # a DATE stays a DATE
        assert unit.lower() == "day", unit
        return (date.fromisoformat(x) + timedelta(days=float(n))).isoformat()
    return _ts(datetime.strptime(x, _TS) + timedelta(**{unit.lower() + "s": float(n)}))


def _try_to_date(v: str | None) -> str | None:
    """TRY_TO_DATE(<varchar>): an ISO date, or a digit string (Snowflake reads it as epoch seconds), else NULL."""
    if v is None:
        return None
    s = str(v)
    if s.isdigit():
        return s
    try:
        return date.fromisoformat(s).isoformat() if len(s) == 10 else None
    except ValueError:
        return None


def _split_part(s: str | None, sep: str, n: int) -> str | None:
    if s is None:
        return None
    parts = s.split(sep)
    return parts[int(n) - 1] if 1 <= int(n) <= len(parts) else ""


def _decode(*args):
    x, rest = args[0], list(args[1:])
    while len(rest) >= 2:
        if rest[0] == x:
            return rest[1]
        rest = rest[2:]
    return rest[0] if rest else None


class _MaxBy:
    def __init__(self) -> None:
        self.best = None

    def step(self, value, key) -> None:
        if key is not None and (self.best is None or key > self.best[0]):
            self.best = (key, value)

    def finalize(self):
        return None if self.best is None else self.best[1]


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)          # explicit BEGIN / COMMIT / ROLLBACK
    con.create_function("SPLIT_PART", 3, _split_part, deterministic=True)
    con.create_function("DATEADD", 3, _dateadd, deterministic=True)
    con.create_function("TRY_TO_DATE", 1, _try_to_date, deterministic=True)
    con.create_function("IFF", 3, lambda c, a, b: a if c else b, deterministic=True)
    con.create_function("DECODE", -1, _decode, deterministic=True)
    con.create_function("SF_LEFT", 2, lambda s, n: None if s is None else str(s)[:int(n)], deterministic=True)
    con.create_function("SF_DATE", 1, lambda s: None if s is None else str(s)[:10], deterministic=True)
    con.create_function("SPLIT", 2, lambda s, sep: json.dumps(str(s).split(sep)), deterministic=True)
    con.create_function("ARRAY_SIZE", 1, lambda a: len(json.loads(a)), deterministic=True)
    con.create_function("NOW_TS", 0, lambda: _ts(_NOW))
    con.create_function("CURRENT_USER_FN", 0, lambda: _OWNER)
    con.create_function("UUID_FN", 0, lambda: next(_UUIDS))
    con.create_aggregate("MAX_BY", 2, _MaxBy)
    con.executescript(f"""
        CREATE TABLE ALERT_EVENTS (EVENT_ID TEXT PRIMARY KEY, RULE_ID TEXT NOT NULL, COMPANY TEXT NOT NULL,
            SEVERITY TEXT NOT NULL, TITLE TEXT NOT NULL, STATUS TEXT NOT NULL, RAISED_AT TEXT NOT NULL,
            DEDUPE_KEY TEXT CHECK (length(DEDUPE_KEY) <= 300));
        CREATE TABLE INCIDENTS (
            INCIDENT_ID TEXT NOT NULL PRIMARY KEY CHECK (length(INCIDENT_ID) <= 80),
            TITLE TEXT NOT NULL CHECK (length(TITLE) <= 300), SEVERITY TEXT NOT NULL CHECK (length(SEVERITY) <= 10),
            STATUS TEXT NOT NULL DEFAULT 'OPEN', COMPANY TEXT NOT NULL DEFAULT 'ALL', DETECTED_AT TEXT NOT NULL,
            ROOT_CAUSE_KIND TEXT, DECLARED_BY TEXT NOT NULL DEFAULT '{_OWNER}' CHECK (length(DECLARED_BY) <= 200));
        CREATE TABLE INCIDENT_MEMBERS (INCIDENT_ID TEXT NOT NULL, MEMBER_KIND TEXT NOT NULL, REF_ID TEXT NOT NULL,
            EVIDENCE_TS TEXT, AUTO_LINKED INTEGER NOT NULL DEFAULT 0,
            LINKED_BY TEXT NOT NULL DEFAULT '{_OWNER}' CHECK (length(LINKED_BY) <= 200),
            LINKED_AT TEXT NOT NULL DEFAULT '{_ts(_NOW)}');
        CREATE TABLE WAREHOUSE_CHANGE_REGISTRY (CHANGE_ID TEXT, WAREHOUSE_NAME TEXT, CHANGE_SEEN_AT TEXT);
        CREATE TABLE OBJECT_CHANGE_REGISTRY (CHANGE_ID TEXT, OBJECT_NAME TEXT, DATABASE_NAME TEXT,
            CHANGE_SEEN_AT TEXT);
        CREATE TABLE FACT_TASK_DAILY (DAY TEXT, DATABASE_NAME TEXT, SCHEMA_NAME TEXT, TASK_NAME TEXT, FAILED INTEGER);
    """)
    return con


def _lit(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


_NAMES_OK = {n.upper() for n in (
    "COALESCE", "SPLIT_PART", "UPPER", "TRIM", "NULLIF", "SF_LEFT", "SF_DATE", "DATEADD", "TRY_TO_DATE", "IFF",
    "MAX_BY", "DECODE", "MAX", "MIN", "COUNT", "SUM", "NOW_TS", "CURRENT_USER_FN", "UUID_FN", "ARRAY_SIZE", "SPLIT",
    "SELECT", "FROM", "AND", "OR", "NOT", "ON", "WHERE", "IN", "EXISTS", "AS", "JOIN", "USING",
    "INCIDENTS", "INCIDENT_MEMBERS")}


def _sqlite(sql: str, binds: dict[str, object] | None = None, bare: frozenset[str] = frozenset()) -> str:
    """Snowflake text -> sqlite text. ``binds`` are :name values; ``bare`` names a Scripting variable may also be
    referenced by without a colon (Snowflake Scripting allows it in expressions)."""
    binds = binds or {}
    s = sql.replace("DBA_MAINT_DB.OVERWATCH.", "")
    s = (s.replace("CURRENT_TIMESTAMP()", "NOW_TS()").replace("CURRENT_USER()", "CURRENT_USER_FN()")
         .replace("UUID_STRING()", "UUID_FN()"))
    s = re.sub(r"\bLEFT\(", "SF_LEFT(", s)
    s = re.sub(r"\bDATE\(", "SF_DATE(", s)

    # ONE pass, so a substituted literal is never re-scanned
    names = r"|(?<![:\w.])\b(" + "|".join(sorted(bare)) + r")\b" if bare else ""
    pattern = re.compile(r"(?<![:\w]):(\w+)" + names)

    def _subst(chunk: str) -> str:
        return pattern.sub(lambda b: _lit(binds[b.group(1) or b.group(2)]), chunk)

    out, last = [], 0
    for m in re.finditer(r"'(?:[^']|'')*'", s):                      # binds only outside string literals
        out.append(_subst(s[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_subst(s[last:]))
    s = "".join(out)
    code = re.sub(r"'(?:[^']|'')*'", "''", s)
    assert "::" not in code and "QUALIFY" not in code, code
    for fn in re.findall(r"\b([A-Za-z_]\w*)\s*\(", code):
        assert fn.upper() in _NAMES_OK, fn
    return s


# ---------------------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------------------
_N = iter(range(1, 100_000))


def _alert(con, key: str, *, company: str = "ALFA", severity: str = "HIGH", status: str = "OPEN",
           hours_ago: float = 1.0, title: str | None = None) -> str:
    eid = f"ev-{next(_N)}"
    con.execute("INSERT INTO ALERT_EVENTS VALUES (?,?,?,?,?,?,?,?)",
                (eid, key.split("|")[0], company, severity, title or f"t {key}", status, _ago(hours_ago), key))
    return eid


def _incident(con, members: list[str], *, status: str = "OPEN", company: str = "ALFA") -> str:
    iid = f"inc-{next(_N)}"
    con.execute("INSERT INTO INCIDENTS (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, "
                "ROOT_CAUSE_KIND, DECLARED_BY) VALUES (?, 'x', 'HIGH', ?, ?, ?, 'UNKNOWN', 'SP_INCIDENT_AUTODECLARE')",
                (iid, status, company, _ago(2)))
    for m in members:
        con.execute("INSERT INTO INCIDENT_MEMBERS (INCIDENT_ID, MEMBER_KIND, REF_ID, AUTO_LINKED, LINKED_BY) "
                    "VALUES (?, 'ALERT', ?, 1, 'SP_INCIDENT_AUTODECLARE')", (iid, m))
    return iid


def _proposals(con, view: str | None = None) -> dict[str, dict]:
    cur = con.execute(_sqlite(_VIEW170 if view is None else view))
    cols = [c[0] for c in cur.description]
    return {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}


# ---------------------------------------------------------------------------------------------------------------
# the proc, walked in its own order
# ---------------------------------------------------------------------------------------------------------------
def _decls(proc: str) -> dict[str, object]:
    block = proc[proc.index("$$\nDECLARE\n") + len("$$\nDECLARE\n"):proc.index("\nBEGIN\n")]
    out: dict[str, object] = {}
    for ln in block.splitlines():
        m = re.fullmatch(r"    (\w+) (STRING|INT|BOOLEAN)(?: DEFAULT (\d+))?;", ln)
        assert m, ln
        out[m.group(1)] = int(m.group(3)) if m.group(3) else None
    return out


def _params(proc: str) -> list[str]:
    sig = proc[proc.index("(") + 1:proc.index(")\nRETURNS")]
    return re.findall(r"(P_\w+) VARCHAR", sig)


def _statements(proc: str) -> list[str]:
    body = proc[proc.index("\nBEGIN\n") + len("\nBEGIN\n"):proc.rindex("\nEXCEPTION\n")]
    tail = proc[proc.rindex("\nEXCEPTION\n"):]
    assert tail.startswith("\nEXCEPTION\n    WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;\nEND;\n$$;")
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("--"))
    for ln in re.sub(r"'(?:[^']|'')*'", "''", code).splitlines():     # ';' only ever ends a statement line
        assert ";" not in ln[:-1] and "--" not in ln, ln
    return [s.strip() for s in code.split(";") if s.strip()]


def _call(con: sqlite3.Connection, proc: str, *args: object) -> str:
    params = _params(proc)
    assert len(args) == len(params), (params, args)
    binds: dict[str, object] = {**dict(zip(params, args, strict=True)), **_decls(proc)}
    bare = frozenset(binds)

    def ev(expr: str):
        return con.execute(_sqlite("SELECT " + expr, binds, bare)).fetchone()[0]

    in_tx, skipping = False, False
    try:
        for st in _statements(proc):
            if skipping:
                skipping = st != "END IF"
                continue
            if st.startswith("IF ("):
                cond, first = re.fullmatch(r"IF \((.*)\) THEN\s+(.*)", st, re.S).groups()
                if not ev(cond):
                    skipping = True
                    continue
                st = first.strip()
            if st == "END IF":
                continue
            if st == "BEGIN TRANSACTION":
                con.execute("BEGIN")
                in_tx = True
            elif st in ("COMMIT", "ROLLBACK"):
                con.execute(st)
                in_tx = False
            elif st.startswith("RETURN "):
                assert not in_tx, "RETURN inside an open transaction"
                return str(ev(st[len("RETURN "):]))
            elif m := re.fullmatch(r"(\w+) := (.*)", st, re.S):
                binds[m.group(1)] = ev(m.group(2))
            elif m := re.fullmatch(r"SELECT (.*?) INTO :(\w+) (FROM .*)", st, re.S):
                binds[m.group(2)] = con.execute(_sqlite(f"SELECT {m.group(1)} {m.group(3)}", binds, bare)).fetchone()[0]
            elif st.startswith("INSERT INTO "):
                con.execute(_sqlite(st, binds, bare))
            else:
                raise AssertionError(f"unhandled statement: {st[:80]}")
        raise AssertionError("the proc fell off its end without a RETURN")
    except Exception:
        if in_tx:
            con.execute("ROLLBACK")
        raise


def _rows(con, sql: str) -> list[tuple]:
    return con.execute(sql).fetchall()


# ===============================================================================================================
# INCIDENT_PROPOSALS
# ===============================================================================================================
def test_every_rule_key_classifies_as_expected_when_executed():
    con = _connect()
    for rule, (key, _) in EXPECTED.items():
        _alert(con, key, title=rule)
    got = {p["PROPOSAL_KEY"].split("|")[0]: p["ENTITY_KIND"] for p in _proposals(con).values()}
    assert got == {rule: kind for rule, (_, kind) in EXPECTED.items()}


def test_v072_left_exh_all_and_the_new_rules_as_scope():
    """The same fixture through V072's view: exactly the R2-093 rules (plus the EXH / ALL keys) differ."""
    con = _connect()
    for rule, (key, _) in EXPECTED.items():
        _alert(con, key, title=rule)
    new = {p["PROPOSAL_KEY"].split("|")[0]: p["ENTITY_KIND"] for p in _proposals(con).values()}
    old = {p["PROPOSAL_KEY"].split("|")[0]: p["ENTITY_KIND"] for p in _proposals(con, _VIEW072).values()}
    changed = {r for r in new if new[r] != old[r]}
    assert changed == {"COST_IDLE_OPPORTUNITY", "COST_SLEEP_POLLING", "PIPE_DT_FAILURES", "PIPE_VOLUME_DROP",
                       "DQ_BREACH", "DQ_SCHEMA_DRIFT", "SEC_FAILED_LOGINS", "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT",
                       "COST_AI_USER_RUNAWAY", "COST_CONTRACT_BREACH", "PIPE_ETL_CYCLE_LATE", "COST_DAILY_CREDITS",
                       "COST_BUDGET_PACE", "COST_FORECAST_BREACH"}
    assert all(old[r] == "SCOPE" for r in changed)


def test_exh_band_folds_into_the_family_account_proposal():
    con = _connect()
    _alert(con, "COST_CONTRACT_BREACH|CRIT|2026-09-28", severity="CRITICAL", hours_ago=30)
    _alert(con, "COST_CONTRACT_BREACH|EXH|2026-09-28", severity="CRITICAL", hours_ago=2)
    new, old = _proposals(con), _proposals(con, _VIEW072)
    assert list(new) == ["COST_CONTRACT_BREACH|ALFA|ACCOUNT|ACCOUNT"]
    assert new["COST_CONTRACT_BREACH|ALFA|ACCOUNT|ACCOUNT"]["ALERTS"] == 2
    assert sorted(old) == ["COST_CONTRACT_BREACH|ALFA|ACCOUNT|ACCOUNT", "COST_CONTRACT_BREACH|ALFA|SCOPE|EXH"]


def _task_fixture(con, *, days: int = 1, task_change: bool = False, rule: str = "PIPE_TASK_FAILURES") -> None:
    for d in range(days):
        day = (_NOW - timedelta(days=1 + d)).date().isoformat()
        _alert(con, f"{rule}|ALFA_EDW.CORE.T_LOAD|{day}", hours_ago=2 + 24 * d)
        con.execute("INSERT INTO FACT_TASK_DAILY VALUES (?, 'ALFA_EDW', 'CORE', 'T_LOAD', 3)", (day,))
    if task_change:
        con.execute("INSERT INTO OBJECT_CHANGE_REGISTRY VALUES ('c1', 'ALFA_EDW.CORE.T_LOAD', 'ALFA_EDW', ?)",
                    (_ago(2.2),))


@pytest.mark.parametrize(("days", "change", "conf"), [(1, False, "LOW"), (2, False, "MEDIUM"), (1, True, "HIGH")])
def test_task_failure_proposal_needs_a_real_change_for_high(days, change, conf):
    con = _connect()
    _task_fixture(con, days=days, task_change=change)
    (p,) = _proposals(con).values()
    assert p["ENTITY_KIND"] == "OBJECT" and p["MATCHED_TASK_FAILURES"] == 3 * days
    assert p["CONFIDENCE"] == conf
    assert f"; task failures (alert source, not corroboration)={3 * days}" in p["EVIDENCE"]
    assert "matched task failures" not in p["EVIDENCE"]
    (old,) = _proposals(con, _VIEW072).values()
    assert old["CONFIDENCE"] == "HIGH"                    # V072: every task-failure proposal read HIGH
    assert f"; matched task failures={3 * days}" in old["EVIDENCE"]


def test_change_regression_on_a_task_keeps_its_task_failure_corroboration():
    con = _connect()
    _task_fixture(con, rule="PERF_CHANGE_REGRESSION")
    (p,) = _proposals(con).values()
    assert p["CONFIDENCE"] == "HIGH" and "; matched task failures=3" in p["EVIDENCE"]


def test_idle_opportunity_now_meets_the_warehouse_change_evidence():
    con = _connect()
    _alert(con, "COST_IDLE_OPPORTUNITY|WH_ALFA_ETL|HIGH|2026-09-28", hours_ago=1)
    con.execute("INSERT INTO WAREHOUSE_CHANGE_REGISTRY VALUES ('w1', 'WH_ALFA_ETL', ?)", (_ago(1.25),))
    (p,) = _proposals(con).values()
    assert (p["ENTITY_KIND"], p["ENTITY_NAME"], p["MATCHED_WH_CHANGES"], p["CONFIDENCE"]) == \
        ("WAREHOUSE", "WH_ALFA_ETL", 1, "HIGH")
    (old,) = _proposals(con, _VIEW072).values()
    assert (old["ENTITY_KIND"], old["MATCHED_WH_CHANGES"]) == ("SCOPE", 0)


def test_view_columns_are_unchanged():
    con = _connect()
    _alert(con, "COST_DAILY_CREDITS|ALL|2026-09-30")
    cols170 = [c[0] for c in con.execute(_sqlite(_VIEW170)).description]
    cols072 = [c[0] for c in con.execute(_sqlite(_VIEW072)).description]
    assert cols170 == cols072 and len(cols170) == 15


# ===============================================================================================================
# SP_INCIDENT_DECLARE (both overloads; V131 as the before)
# ===============================================================================================================
def _declare(con, proc: str, key: str, actor: object = "JDOE", company: str = "ALFA") -> str:
    args: tuple = ("Contract exhausted", "critical", company, key)
    return _call(con, proc, *((*args, actor) if proc is _P5 else args))


@pytest.mark.parametrize("which", ["4", "5"])
def test_declare_links_members_and_returns_ok(which):
    from app.ui.pages.control_room import _declare_verdict
    proc = _P4 if which == "4" else _P5
    con = _connect()
    a1 = _alert(con, "PIPE_TASK_FAILURES|DB.SCH.T|2026-09-30", hours_ago=3)
    a2 = _alert(con, "PIPE_TASK_FAILURES|DB.SCH.T|2026-10-01", hours_ago=1)
    _alert(con, "PIPE_TASK_FAILURES|DB.SCH.OTHER|2026-10-01")                      # another entity
    _alert(con, "PIPE_TASK_FAILURES|DB.SCH.T|2026-09-28", hours_ago=60)           # outside the 48 h window
    _alert(con, "PIPE_TASK_FAILURES|DB.SCH.T|2026-10-01", company="TREXIS")        # another company
    msg = _declare(con, proc, "PIPE_TASK_FAILURES|ALFA|OBJECT|DB.SCH.T")
    ((iid, by, sev),) = _rows(con, "SELECT INCIDENT_ID, DECLARED_BY, SEVERITY FROM INCIDENTS")
    assert msg == f"OK: declared {iid} with 2 member(s) linked"
    assert _declare_verdict(msg) == ("declared", 2)
    members = _rows(con, "SELECT REF_ID, LINKED_BY, AUTO_LINKED FROM INCIDENT_MEMBERS ORDER BY REF_ID")
    assert [m[0] for m in members] == sorted([a1, a2])
    who = "JDOE" if which == "5" else _OWNER                                     # the 4-arg keeps crediting the owner
    assert by == who and {m[1] for m in members} == {who} and sev == "CRITICAL"
    assert {m[2] for m in members} == {0}


@pytest.mark.parametrize(("actor", "want"), [("", _OWNER), ("   ", _OWNER), (None, _OWNER),
                                             ("  JDOE ", "JDOE"), ("X" * 250, "X" * 200)])
def test_actor_falls_back_to_current_user_and_fits_the_columns(actor, want):
    con = _connect()
    _alert(con, "COST_DAILY_CREDITS|ALL|2026-09-30")
    msg = _declare(con, _P5, "COST_DAILY_CREDITS|ALFA|ACCOUNT|ACCOUNT", actor=actor)
    assert msg.startswith("OK: declared ")
    assert _rows(con, "SELECT DECLARED_BY FROM INCIDENTS") == [(want,)]
    assert _rows(con, "SELECT DISTINCT LINKED_BY FROM INCIDENT_MEMBERS") == [(want,)]


@pytest.mark.parametrize("which", ["4", "5"])
def test_a_declare_whose_alerts_cleared_rolls_back(which):
    """R2-030: every proposal alert left OPEN/ACK between the 30 s-cached read and the CALL."""
    from app.ui.pages.control_room import _declare_verdict
    proc = _P4 if which == "4" else _P5
    con = _connect()
    _alert(con, "PERF_QUEUED_MINUTES|WH_ALFA_ETL|2026-10-01", status="RESOLVED")
    msg = _declare(con, proc, "PERF_QUEUED_MINUTES|ALFA|WAREHOUSE|WH_ALFA_ETL")
    assert msg == "NOOP: no open alerts left to link - nothing declared"
    assert _declare_verdict(msg) == ("no_alerts", None)
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENTS") == [(0,)]
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENT_MEMBERS") == [(0,)]


def test_v131_committed_the_empty_incident_and_the_app_says_so():
    from app.ui.pages.control_room import _declare_verdict
    con = _connect()
    _alert(con, "PERF_QUEUED_MINUTES|WH_ALFA_ETL|2026-10-01", status="RESOLVED")
    msg = _call(con, _P131, "t", "HIGH", "ALFA", "PERF_QUEUED_MINUTES|ALFA|WAREHOUSE|WH_ALFA_ETL")
    assert msg == "DECLARED: 0 member(s) linked"
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENTS") == [(1,)]                  # the defect V170 removes
    assert _declare_verdict(msg) == ("empty", 0)


@pytest.mark.parametrize("which", ["4", "5"])
def test_family_already_open_writes_nothing(which):
    from app.ui.pages.control_room import _declare_verdict
    proc = _P4 if which == "4" else _P5
    con = _connect()
    held = _alert(con, "COST_CONTRACT_BREACH|CRIT|2026-09-28", severity="CRITICAL", hours_ago=30)
    _incident(con, [held], status="MITIGATED")
    _alert(con, "COST_CONTRACT_BREACH|EXH|2026-09-28", severity="CRITICAL")
    msg = _declare(con, proc, "COST_CONTRACT_BREACH|ALFA|ACCOUNT|ACCOUNT")
    assert msg == "NOOP: this family already has an open incident"
    assert _declare_verdict(msg) == ("family_open", None)
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENTS") == [(1,)]
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENT_MEMBERS") == [(1,)]


def test_the_exh_proposal_v072_offered_opened_a_second_incident():
    """R2-093 end to end: declare from what each view proposes after the CRIT was auto-declared."""
    con = _connect()
    held = _alert(con, "PIPE_ETL_CYCLE_LATE|CRIT|2026-09-30", severity="CRITICAL", hours_ago=6)
    _incident(con, [held])
    _alert(con, "PIPE_ETL_CYCLE_LATE|EXH|2026-09-30", severity="HIGH", hours_ago=1)
    (old_key,) = _proposals(con, _VIEW072)
    (new_key,) = _proposals(con)
    assert old_key == "PIPE_ETL_CYCLE_LATE|ALFA|SCOPE|EXH" and new_key == "PIPE_ETL_CYCLE_LATE|ALFA|ACCOUNT|ACCOUNT"
    assert _declare(con, _P5, new_key) == "NOOP: this family already has an open incident"
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENTS") == [(1,)]
    assert _declare(con, _P5, old_key).startswith("OK: declared ")                 # the old key's entity guard
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENTS") == [(2,)]                  # -> the duplicate incident


def test_invalid_key_is_refused_and_classified_failed():
    from app.ui.pages.control_room import _declare_verdict
    con = _connect()
    for proc in (_P4, _P5):
        msg = _declare(con, proc, "  ")
        assert msg == "INVALID: proposal key is required" and _declare_verdict(msg) == ("failed", None)
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENTS") == [(0,)]


def test_a_failing_statement_rolls_the_whole_declare_back():
    """The EXCEPTION handler: an over-wide title would never reach here (LEFT 300), so force a members failure."""
    con = _connect()
    _alert(con, "COST_DAILY_CREDITS|ALL|2026-09-30")
    con.execute("CREATE TRIGGER boom BEFORE INSERT ON INCIDENT_MEMBERS BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        _declare(con, _P5, "COST_DAILY_CREDITS|ALFA|ACCOUNT|ACCOUNT")
    assert _rows(con, "SELECT COUNT(*) FROM INCIDENTS") == [(0,)]


def test_the_harness_walks_every_statement_of_both_overloads():
    """Guard the guard: each overload has exactly the statements the walker knows, in V131's order."""
    kind_re = re.compile(r"IF|END IF|BEGIN TRANSACTION|COMMIT|ROLLBACK|RETURN|INSERT INTO \w+|SELECT|\w+ :=")
    shapes = [[kind_re.match(st.replace("DBA_MAINT_DB.OVERWATCH.", "")).group(0) for st in _statements(proc)]
              for proc in (_P131, _P4, _P5)]
    base, four, five = shapes
    assert four == five[:3] + five[4:] and five[3] == "v_actor :="                  # the one extra assignment
    added = ["IF", "RETURN", "END IF"]
    i = four.index("COMMIT")
    assert four[:i - 3] + four[i:] == base and four[i - 3:i] == added
