"""Executed harness for V168's SP_ALERT_SCAN deltas -- arm [14] PIPE_COPY_FAILURES (R2-035), arm [18]
SEC_NEW_ADMIN_NETWORK (R2-036 + R2-039, composed), the V067 supersede sweep (R2-039 D4) and the V091 auto-clear
sweep (R2-034).

Every statement runs from the V168 migration's OWN text (never a hand-written twin) in an in-memory sqlite, and the
same fixture runs V162's text where a test needs teeth (the defect, reproduced). Clocks are integer milliseconds
of Central wall-clock time, so CONVERT_TIMEZONE('America/Chicago', x) is the identity and TO_DATE(x) is the
Central day; a DATE is 'YYYY-MM-DD'. Comments are dropped before translation; any '::' cast, CONVERT_TIMEZONE,
QUALIFY or :bind left over raises (fail closed). ALERT_EVENTS carries its V004 widths as CHECKs.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from tests._source import read
from tests.migrations.test_v157_alert_scan_self_watch_idle_push import _derived_to_cte
from tests.migrations.test_v163_harness import _lex

_V168 = read("snowflake/migrations/V168__alert_scan_hourly_keys_and_sweeps.sql")
_V162 = read("snowflake/migrations/V162__security_takeover_admin_grant.sql")
_COPY, _NET = "PIPE_COPY_FAILURES", "SEC_NEW_ADMIN_NETWORK"


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _arm(h: str, start: str, end: str) -> str:
    block = _between(h, start, end)
    return block[block.index("        WITH cfg AS ("):block.index(";\n    EXCEPTION")]


def _update(h: str, head: str) -> str:
    i = h.index(head)
    return h[i:h.index(";\n", i)]


_H, _H162 = _proc(_V168, "SP_ALERT_SCAN()"), _proc(_V162, "SP_ALERT_SCAN()")
_A14 = ("    -- [14] PIPE_COPY_FAILURES", "    -- [17] COST_DEPT_BUDGET_PACE")
_A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
_ARM14, _ARM14_162 = _arm(_H, *_A14), _arm(_H162, *_A14)
_ARM18, _ARM18_162 = _arm(_H, *_A18), _arm(_H162, *_A18)
_SUPERSEDE = _update(_H, "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo\n")
_AUTOCLEAR_HEAD = ("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev\n           SET STATUS = 'RESOLVED', RESOLVED_AT = "
                   "CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'")
_AUTOCLEAR, _AUTOCLEAR_162 = _update(_H, _AUTOCLEAR_HEAD), _update(_H162, _AUTOCLEAR_HEAD)

# -- clocks ---------------------------------------------------------------------------------------------------
_EPOCH = datetime(1970, 1, 1)
_UNIT_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


def _ms(d: datetime) -> int:
    return round((d - _EPOCH).total_seconds() * 1000)


def _dt(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=int(ms))


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm)


def _dateadd(unit, n, x):
    if unit is None or n is None or x is None:
        return None
    u = str(unit).lower()
    if isinstance(x, str):                        # a DATE stays a DATE
        assert u == "day", u
        return (date.fromisoformat(x[:10]) + timedelta(days=int(n))).isoformat()
    return int(x) + int(n) * _UNIT_MS[u]


def _to_date(x):
    if x is None:
        return None
    return x[:10] if isinstance(x, str) else _dt(x).date().isoformat()


def _to_varchar(x, fmt=None):
    if x is None:
        return None
    assert isinstance(x, str), x                  # only DATEs are rendered by these arms
    assert fmt in (None, "YYYY-MM-DD"), fmt
    return x


def _nullsafe(fn):
    return lambda *a: None if any(v is None for v in a) else fn(*a)


class _CountIf:
    def __init__(self) -> None:
        self.n = 0

    def step(self, v) -> None:
        self.n += 1 if v else 0

    def finalize(self) -> int:
        return self.n


_SCHEMAS = {
    "ALERT_CONFIG": "RULE_ID, SEVERITY, ENABLED, THRESHOLD_NUM, CLEAR_THRESHOLD_NUM, AUTO_CLEAR_ENABLED",
    "ALERT_EVENTS": ("EVENT_ID, RULE_ID, COMPANY, SEVERITY, TITLE CHECK (length(TITLE) <= 300), "
                     "DETAIL CHECK (length(DETAIL) <= 2000), METRIC_VALUE, "
                     "DEDUPE_KEY CHECK (length(DEDUPE_KEY) <= 300), STATUS, RESOLUTION_KIND, RAISED_AT, RESOLVED_AT"),
    "COPY_HISTORY": ("TABLE_CATALOG_NAME, TABLE_SCHEMA_NAME, TABLE_NAME, PIPE_NAME, LAST_LOAD_TIME, STATUS, "
                     "FIRST_ERROR_MESSAGE"),
    "LOGIN_HISTORY": "USER_NAME, EVENT_TIMESTAMP, IS_SUCCESS, CLIENT_IP, FIRST_AUTHENTICATION_FACTOR",
    "GRANTS_TO_USERS": "GRANTEE_NAME, ROLE, DELETED_ON",
    "FACT_QUERY_HOURLY": ("HOUR_TS, COMPANY, WAREHOUSE_NAME, QUERY_COUNT, FAILED_COUNT, QUEUED_SEC_SUM, "
                          "SPILL_REMOTE_GB"),
}


def _connect(tables: dict, now: int) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    funcs = {
        "NOW_TS": (0, lambda: now), "DATEADD": (3, _dateadd), "TO_DATE": (1, _to_date),
        "TO_VARCHAR": (-1, _to_varchar), "IFF": (3, lambda c, a, b: a if c else b),
        "LEFT": (2, _nullsafe(lambda s, n: str(s)[:max(int(n), 0)])),
        "SPLIT_PART": (3, _nullsafe(lambda s, d, n: (str(s).split(d) + [""] * int(n))[int(n) - 1])),
        "COMPANY_FOR_DATABASE": (1, lambda db: "ALFA"),
    }
    for name, (narg, fn) in funcs.items():
        con.create_function(name, narg, fn)
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    for table, cols in _SCHEMAS.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
        names = [c.split()[0] for c in cols.split(", ")]
        for row in tables.get(table, ()):
            assert set(row) <= set(names), (table, set(row) - set(names))
            con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))
    return con


def _sq(sql: str) -> str:
    """Snowflake -> sqlite, minimal and fail-closed (comments dropped first, string literals untouched)."""
    s = "".join(t for kind, t in _lex(sql) if kind != "comment")
    s = s.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    s = s.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())", "NOW_TS()")
    s = s.replace("CURRENT_TIMESTAMP()", "NOW_TS()").replace("CURRENT_DATE()", "TO_DATE(NOW_TS())")
    s = re.sub(r"CONVERT_TIMEZONE\('America/Chicago', ([\w.]+)\)", r"\1", s)
    s = re.sub(r"\bUPDATE (\w+) (\w+)\n", r"UPDATE \1 AS \2\n", s)
    s = _derived_to_cte(s)
    code = "".join(t for kind, t in _lex(s) if kind == "code")
    assert "::" not in code and "QUALIFY" not in code and "CONVERT_TIMEZONE" not in code, code
    assert not re.search(r"(?<![:\w]):\w", code), code
    return s


_EVENT_COLS = ("RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE", "DEDUPE_KEY")
_EID = itertools.count(1)


def _cfg(rule: str, threshold, *, clear=None, auto: int = 0, severity: str = "HIGH", enabled: int = 1) -> dict:
    return {"RULE_ID": rule, "SEVERITY": severity, "ENABLED": enabled, "THRESHOLD_NUM": threshold,
            "CLEAR_THRESHOLD_NUM": clear, "AUTO_CLEAR_ENABLED": auto}


class _Scan:
    """An event store plus the fixtures; each .arm() run INSERTs what the arm raises (RAISED_AT = now)."""

    def __init__(self, cfg: list[dict]) -> None:
        self.cfg = cfg
        self.events: list[dict] = []
        self.copy: list[dict] = []
        self.logins: list[dict] = []
        self.grants: list[dict] = [{"GRANTEE_NAME": u, "ROLE": "ACCOUNTADMIN", "DELETED_ON": None}
                                   for u in ("JDOE", "first.last.name", "U" * 255)]
        self.hourly: list[dict] = []

    def _con(self, now: datetime) -> sqlite3.Connection:
        return _connect({"ALERT_CONFIG": self.cfg, "ALERT_EVENTS": self.events, "COPY_HISTORY": self.copy,
                         "LOGIN_HISTORY": self.logins, "GRANTS_TO_USERS": self.grants,
                         "FACT_QUERY_HOURLY": self.hourly}, _ms(now))

    def arm(self, sql: str, now: datetime) -> list[dict]:
        cur = self._con(now).execute(_sq(sql))
        assert tuple(c[0] for c in cur.description) == _EVENT_COLS
        rows = [dict(zip(_EVENT_COLS, r, strict=True)) for r in cur.fetchall()]
        con = self._con(now)
        for r in rows:                             # the V004 widths as CHECK constraints
            con.execute("INSERT INTO ALERT_EVENTS (" + ", ".join(_EVENT_COLS) + ") VALUES ("
                        + ", ".join("?" * len(_EVENT_COLS)) + ")", tuple(r.values()))
            self.events.append(dict(r, EVENT_ID=f"e{next(_EID)}", STATUS="OPEN", RESOLUTION_KIND=None,
                                    RAISED_AT=_ms(now), RESOLVED_AT=None))
        return rows

    def sweep(self, sql: str, now: datetime) -> None:
        con = self._con(now)
        con.execute(_sq(sql))
        cur = con.execute("SELECT * FROM ALERT_EVENTS")
        cols = [c[0] for c in cur.description]
        self.events = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def state(self) -> dict:
        return {e["DEDUPE_KEY"]: (e["STATUS"], e["RESOLUTION_KIND"]) for e in self.events}


# ============================================================================================================
# [14] PIPE_COPY_FAILURES (R2-035)
# ============================================================================================================
_D = date(2026, 9, 29)               # a Tuesday


def _fail(at: datetime, n: int = 1, tbl: str = "CLAIMS") -> list[dict]:
    return [{"TABLE_CATALOG_NAME": "TRXS_DW", "TABLE_SCHEMA_NAME": "STAGING", "TABLE_NAME": tbl,
             "PIPE_NAME": "P_CLAIMS", "LAST_LOAD_TIME": _ms(at + timedelta(seconds=i)), "STATUS": "Load failed",
             "FIRST_ERROR_MESSAGE": "Numeric value 'x' is not recognized"} for i in range(n)]


def _hourly(start: datetime, end: datetime) -> list[datetime]:
    """The :07 hourly scans from start through end."""
    t = start.replace(minute=7, second=0, microsecond=0)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def _key14(band: str, day: date, tbl: str = "CLAIMS") -> str:
    return f"{_COPY}|TRXS_DW.STAGING.{tbl}|{band}|{day.isoformat()}"


def test_arm14_one_event_per_failure_day_across_midnight_and_a_resolve():
    """R2-035 (a): 3 files fail at 14:00 on D; hourly scans run through D+1 23:07; an operator resolves the event
    ACTIONED at 17:00 on D. Exactly ONE event, keyed by the failure day. V162's text re-raises after midnight."""
    def run(arm: str) -> list[dict]:
        s = _Scan([_cfg(_COPY, 0)])
        s.copy = _fail(_at(_D, 14), 3)
        raised = []
        for t in _hourly(_at(_D, 14, 30), _at(_D + timedelta(days=1), 23, 30)):
            raised += s.arm(arm, t)
            if t.hour == 17 and t.date() == _D:
                for e in s.events:
                    e.update(STATUS="RESOLVED", RESOLUTION_KIND="ACTIONED")
        return raised

    (ev,) = run(_ARM14)
    assert ev["DEDUPE_KEY"] == _key14("WARN", _D) and ev["SEVERITY"] == "HIGH" and ev["METRIC_VALUE"] == 3
    assert ev["TITLE"] == f"TRXS_DW.STAGING.CLAIMS: 3 failed file load(s) on {_D.isoformat()}"
    assert ev["COMPANY"] == "ALFA" and "pipe P_CLAIMS" in ev["DETAIL"]
    # teeth: V162's rolling-24h, scan-day key raised the same files again after midnight
    old = [e["DEDUPE_KEY"] for e in run(_ARM14_162)]
    assert len(old) == 2 and old[0].endswith(_D.isoformat())


def test_arm14_band_only_escalates_within_a_day_and_never_steps_down_after_midnight():
    """R2-035 (b): 6 files at 10:00 and 6 at 10:30 on D -> WARN then CRIT (the V067 sweep supersedes the WARN); on
    D+1, with the CRIT resolved, no WARN|D is minted (the whole-day count is still 12)."""
    s = _Scan([_cfg(_COPY, 0)])
    s.copy = _fail(_at(_D, 10), 6)
    assert [e["DEDUPE_KEY"] for e in s.arm(_ARM14, _at(_D, 10, 7))] == [_key14("WARN", _D)]
    s.copy += _fail(_at(_D, 10, 30), 6)
    (crit,) = s.arm(_ARM14, _at(_D, 11, 7))
    assert crit["DEDUPE_KEY"] == _key14("CRIT", _D) and crit["SEVERITY"] == "CRITICAL"
    s.sweep(_SUPERSEDE, _at(_D, 11, 7))
    assert s.state() == {_key14("WARN", _D): ("RESOLVED", "SUPERSEDED"), _key14("CRIT", _D): ("OPEN", None)}
    for e in s.events:
        e.update(STATUS="RESOLVED", RESOLUTION_KIND="ACTIONED")
    for t in _hourly(_at(_D + timedelta(days=1), 0, 0), _at(_D + timedelta(days=1), 23, 30)):
        assert s.arm(_ARM14, t) == [], t


def test_arm14_a_late_row_keys_its_real_day_and_a_new_day_gets_a_new_key():
    """R2-035 (c) a 23:50 failure on D first visible at 01:07 on D+1 keys D; (d) a fresh failure on D+1 keys D+1;
    a failure from D-2 is outside the window (yesterday + today only)."""
    d1 = _D + timedelta(days=1)
    s = _Scan([_cfg(_COPY, 0)])
    s.copy = _fail(_at(_D - timedelta(days=2), 9), 2, tbl="OLD")
    assert s.arm(_ARM14, _at(_D, 23, 7)) == []
    s.copy += _fail(_at(_D, 23, 50))
    assert [e["DEDUPE_KEY"] for e in s.arm(_ARM14, _at(d1, 1, 7))] == [_key14("WARN", _D)]
    s.copy += _fail(_at(d1, 9))
    assert [e["DEDUPE_KEY"] for e in s.arm(_ARM14, _at(d1, 9, 7))] == [_key14("WARN", d1)]
    assert s.arm(_ARM14, _at(d1, 10, 7)) == []
    # the threshold still gates (allowed failures)
    s2 = _Scan([_cfg(_COPY, 3)])
    s2.copy = _fail(_at(_D, 9), 3)
    assert s2.arm(_ARM14, _at(_D, 10, 7)) == []
    s2.copy += _fail(_at(_D, 9, 30))
    assert [e["METRIC_VALUE"] for e in s2.arm(_ARM14, _at(_D, 10, 7))] == [4]


# ============================================================================================================
# [18] SEC_NEW_ADMIN_NETWORK (R2-036 + R2-039)
# ============================================================================================================
_SEP = date(2026, 9, 30)


def _login(user: str, at: datetime, ok: bool, ip: str = "203.0.113.9") -> dict:
    return {"USER_NAME": user, "EVENT_TIMESTAMP": _ms(at), "IS_SUCCESS": "YES" if ok else "NO", "CLIENT_IP": ip,
            "FIRST_AUTHENTICATION_FACTOR": "PASSWORD"}


def _key18(user: str, ip: str, day: date, failed: bool = False) -> str:
    return f"{_NET}|{user[:200]}|{ip}{'|FAILED' if failed else ''}|{day.isoformat()}"


def _legacy(s: _Scan, key: str, raised: datetime, status: str = "OPEN") -> None:
    s.events.append({"EVENT_ID": f"x{next(_EID)}", "RULE_ID": key.split("|", 1)[0], "COMPANY": "ALL",
                     "SEVERITY": "HIGH", "TITLE": "old", "DETAIL": None, "METRIC_VALUE": 1, "DEDUPE_KEY": key,
                     "STATUS": status, "RESOLUTION_KIND": None, "RAISED_AT": _ms(raised), "RESOLVED_AT": None})


def test_arm18_failures_only_attempts_say_so_and_carry_a_failed_key():
    """R2-039 (a): three IS_SUCCESS='NO' attempts from a new IP raise one event with the failed wording."""
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("JDOE", _at(_SEP, 3, m), False) for m in (1, 2, 3)]
    (ev,) = s.arm(_ARM18, _at(_SEP, 5, 7))
    assert ev["TITLE"] == "JDOE: 3 failed login attempt(s) from new network 203.0.113.9 (0 successful)"
    assert ev["METRIC_VALUE"] == 3 and ev["DEDUPE_KEY"] == _key18("JDOE", "203.0.113.9", _SEP, failed=True)
    assert "successful 0 of 3 attempt(s)" in ev["DETAIL"] and "No attempt got in" in ev["DETAIL"]
    assert s.arm(_ARM18, _at(_SEP, 6, 7)) == []                                  # (e) no duplicate
    # V162 said 'logged in' for the same attempts
    s162 = _Scan([_cfg(_NET, 1)])
    s162.logins = s.logins
    (old,) = s162.arm(_ARM18_162, _at(_SEP, 5, 7))
    assert old["TITLE"] == "JDOE logged in from new network 203.0.113.9"


def test_arm18_a_later_success_raises_its_own_event_and_supersedes_the_failed_one():
    """R2-039 (b) + the composition fix: the success's key is not swallowed by the failures-only key (an exact
    base compare, not a prefix one), and the V067 sweep then supersedes the failed event."""
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("JDOE", _at(_SEP, 3, m), False) for m in (1, 2, 3)]
    (failed,) = s.arm(_ARM18, _at(_SEP, 5, 7))
    s.sweep(_SUPERSEDE, _at(_SEP, 5, 7))
    assert s.state() == {failed["DEDUPE_KEY"]: ("OPEN", None)}
    s.logins.append(_login("JDOE", _at(_SEP, 9), True))
    (ok,) = s.arm(_ARM18, _at(_SEP, 10, 7))
    assert ok["TITLE"] == "JDOE logged in from new network 203.0.113.9"
    assert ok["DEDUPE_KEY"] == _key18("JDOE", "203.0.113.9", _SEP) and ok["METRIC_VALUE"] == 4
    assert "successful 1 of 4 attempt(s)" in ok["DETAIL"] and "Expected after travel/VPN" in ok["DETAIL"]
    s.sweep(_SUPERSEDE, _at(_SEP, 10, 7))
    assert s.state() == {failed["DEDUPE_KEY"]: ("RESOLVED", "SUPERSEDED"), ok["DEDUPE_KEY"]: ("OPEN", None)}
    assert s.arm(_ARM18, _at(_SEP, 11, 7)) == []
    # teeth: R2-036's STARTSWITH guard would have swallowed the success (the failed key starts with its base)
    starts = _ARM18.replace(
        "(e.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')",
        "(e.DEDUPE_KEY || '|' LIKE LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10) || '%'", 1)
    assert starts != _ARM18
    s2 = _Scan([_cfg(_NET, 1)])
    s2.logins = [_login("JDOE", _at(_SEP, 3, m), False) for m in (1, 2, 3)]
    s2.arm(starts, _at(_SEP, 5, 7))
    s2.logins.append(_login("JDOE", _at(_SEP, 9), True))
    assert s2.arm(starts, _at(_SEP, 10, 7)) == []


def test_arm18_success_only_pair_keeps_the_v162_title_and_a_dated_key():
    """R2-039 (c), (d): a success-only pair; an IP already in the 90-day baseline raises nothing."""
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("first.last.name", _at(_SEP, 8), True, ip="198.51.100.7"),
                _login("JDOE", _at(_SEP - timedelta(days=30), 8), True), _login("JDOE", _at(_SEP, 8), True)]
    (ev,) = s.arm(_ARM18, _at(_SEP, 9, 7))
    assert ev["TITLE"] == "first.last.name logged in from new network 198.51.100.7"
    assert ev["DEDUPE_KEY"] == _key18("first.last.name", "198.51.100.7", _SEP)
    # a user outside the three admin roles never raises
    s.logins.append(_login("NOBODY", _at(_SEP, 8), True, ip="10.9.9.9"))
    assert s.arm(_ARM18, _at(_SEP, 10, 7)) == []


def test_arm18_a_network_quiet_for_90_days_alerts_again():
    """R2-036 (a): a January event (resolved EXPECTED), the IP quiet until September -- the September scan raises.
    V162's undated key matched the January event forever."""
    jan = date(2026, 1, 15)
    logins = [_login("JDOE", _at(jan, 8), True), _login("JDOE", _at(_SEP, 8), True)]
    s = _Scan([_cfg(_NET, 1)])
    _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(jan, 9), status="RESOLVED")
    s.logins = logins
    (ev,) = s.arm(_ARM18, _at(_SEP, 9, 7))
    assert ev["DEDUPE_KEY"] == _key18("JDOE", "203.0.113.9", _SEP)
    # the next episode after a dated one too (the previous episode is 89+ days old: outside the 48h guard)
    s.logins.append(_login("JDOE", _at(date(2027, 1, 20), 8), True))
    (again,) = s.arm(_ARM18, _at(date(2027, 1, 20), 9, 7))
    assert again["DEDUPE_KEY"] == _key18("JDOE", "203.0.113.9", date(2027, 1, 20))
    # teeth: V162
    s162 = _Scan([_cfg(_NET, 1)])
    _legacy(s162, f"{_NET}|JDOE|203.0.113.9", _at(jan, 9), status="RESOLVED")
    s162.logins = logins
    assert s162.arm(_ARM18_162, _at(_SEP, 9, 7)) == []


def test_arm18_one_event_per_episode_across_hourly_scans_and_central_midnight():
    """R2-036 (b), (c): 24 hourly scans of one episode give one event; a late, EARLIER login that moves FIRST_SEEN
    across Central midnight (a new date tail) is the same episode and stays one event."""
    d1 = _SEP + timedelta(days=1)
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("JDOE", _at(d1, 0, 10), True)]
    raised = []
    for t in _hourly(_at(d1, 0, 30), _at(d1, 23, 30)):
        if t.hour == 2:
            s.logins.append(_login("JDOE", _at(_SEP, 23, 50), False))         # late ACCOUNT_USAGE row
        raised += s.arm(_ARM18, t)
    assert [e["DEDUPE_KEY"] for e in raised] == [_key18("JDOE", "203.0.113.9", d1)]
    # without the 48h guard the late row would have minted a second (yesterday-dated) key
    no_guard = _ARM18.replace("               OR (e.RULE_ID = b.RULE_ID\n", "               OR (1 = 0\n", 1)
    assert no_guard != _ARM18
    s2 = _Scan([_cfg(_NET, 1)])
    s2.logins = [_login("JDOE", _at(d1, 0, 10), True)]
    s2.arm(no_guard, _at(d1, 1, 7))
    s2.logins.append(_login("JDOE", _at(_SEP, 23, 50), False))
    assert [e["DEDUPE_KEY"] for e in s2.arm(no_guard, _at(d1, 3, 7))] == [_key18("JDOE", "203.0.113.9", _SEP)]


def test_arm18_a_pre_v168_undated_key_blocks_its_own_episode_only():
    """R2-036 (d) + the transition tweak: an undated V162 key raised under 48h ago blocks the same episode (success
    or failures-only); one raised 89+ days ago does not."""
    for ok in (True, False):
        s = _Scan([_cfg(_NET, 1)])
        _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP, 4, 7))
        s.logins = [_login("JDOE", _at(_SEP, 3), ok)]
        assert s.arm(_ARM18, _at(_SEP, 5, 7)) == [], ok
    s = _Scan([_cfg(_NET, 1)])
    _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP - timedelta(days=89), 4, 7))
    s.logins = [_login("JDOE", _at(_SEP, 3), True)]
    assert len(s.arm(_ARM18, _at(_SEP, 5, 7))) == 1
    # teeth: without the '|FAILED' strip, a failures-only episode re-raised once over its V162 undated key
    raw = _ARM18.replace("REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')",
                         "LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11)", 1)
    assert raw != _ARM18
    s = _Scan([_cfg(_NET, 1)])
    _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP, 4, 7))
    s.logins = [_login("JDOE", _at(_SEP, 3), False)]
    assert len(s.arm(raw, _at(_SEP, 5, 7))) == 1


def _flat_header() -> str:
    head = _V168[:_V168.index("EXECUTE IMMEDIATE")]
    return " ".join(" ".join(ln[2:].strip() for ln in head.splitlines() if ln.startswith("--")).split())


def test_apply_window_suppressions_are_codified_and_named_in_the_first_run_header():
    """Review r1: two once-only apply-window suppressions and one standing same-day behaviour, all in FIRST RUN.
    (1) [14]: V162 re-minted D's afternoon failures after midnight as WARN|D+1 (the R2-035 carry-over, resolved
    NOISE); V168 is applied on D+1; a new 14:00 failure on D+1 keys WARN|D+1 too and folds into that event.
    (2) [18]: a failures-only pair raised by V162 (undated key) just before the apply; its success an hour later
    strips to the same undated base inside 48h, so it raises nothing during the transition."""
    d1 = _D + timedelta(days=1)
    s = _Scan([_cfg(_COPY, 0)])
    s.copy = _fail(_at(_D, 14), 3)
    for t in _hourly(_at(_D, 14, 30), _at(d1, 0, 30)):
        s.arm(_ARM14_162, t)
    assert [e["DEDUPE_KEY"] for e in s.events] == [_key14("WARN", _D), _key14("WARN", d1)]   # V162: D + carry-over
    for e in s.events:
        e.update(STATUS="RESOLVED", RESOLUTION_KIND="NOISE")
    s.copy += _fail(_at(d1, 14), 2)
    assert s.arm(_ARM14, _at(d1, 14, 7)) == []                                     # (1) folded into WARN|D+1
    s.copy += _fail(_at(d1, 15), 8)                                                # ...until the day reaches 10
    assert [e["DEDUPE_KEY"] for e in s.arm(_ARM14, _at(d1, 15, 7))] == [_key14("CRIT", d1)]
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("JDOE", _at(_SEP, 3), False)]
    (v162,) = s.arm(_ARM18_162, _at(_SEP, 4, 7))
    assert v162["DEDUPE_KEY"] == f"{_NET}|JDOE|203.0.113.9" and "logged in" in v162["TITLE"]
    s.logins.append(_login("JDOE", _at(_SEP, 5), True))
    assert s.arm(_ARM18, _at(_SEP, 6, 7)) == []                                    # (2) the success is swallowed
    flat = _flat_header()
    for phrase in ("Two apply-window suppressions, once each:",
                   "folds the apply day's new failures of the same band into that event",
                   "PREFLIGHT P168.2 flags those keys",
                   "a success that follows a failures-only V162 event inside its 48h does not raise its own event",
                   "PREFLIGHT P168.4 (second grid) lists the candidates",
                   "Standing (as under V162):",
                   "stays suppressed until the next Central day, unless it crosses WARN -> CRIT"):
        assert phrase in flat, phrase


def test_arm18_ip_prefixes_never_collide():
    """R2-036 (e): 10.0.0.1 never blocks 10.0.0.12 (and back), in either outcome."""
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("JDOE", _at(_SEP, 3), True, ip="10.0.0.1")]
    s.arm(_ARM18, _at(_SEP, 4, 7))
    s.logins += [_login("JDOE", _at(_SEP, 5), True, ip="10.0.0.12"), _login("JDOE", _at(_SEP, 5), False, ip="10.0.0.2")]
    got = sorted(e["DEDUPE_KEY"] for e in s.arm(_ARM18, _at(_SEP, 6, 7)))
    assert got == sorted([_key18("JDOE", "10.0.0.12", _SEP), _key18("JDOE", "10.0.0.2", _SEP, failed=True)])


def test_arm18_long_names_fit_the_v004_widths():
    """R2-036 (g) / R2-039 (f): a 255-character user name keeps the key <= 300 (LEFT 200) and the TITLE <= 300."""
    user = "U" * 255
    s = _Scan([_cfg(_NET, 1)])
    ip = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
    s.logins = [_login(user, _at(_SEP, 3, m), False, ip=ip) for m in range(3)]
    (ev,) = s.arm(_ARM18, _at(_SEP, 5, 7))                    # the CHECKs would reject an overflow
    assert ev["DEDUPE_KEY"] == _key18(user, ip, _SEP, failed=True) and len(ev["DEDUPE_KEY"]) <= 300
    assert len(ev["TITLE"]) == 300                             # LEFT(.., 300) cut the failed wording


# ============================================================================================================
# V067 supersede (R2-039 D4): scoped to SEC_NEW_ADMIN_NETWORK, a 48h episode, failed -> success only
# ============================================================================================================
def test_supersede_closes_only_a_failed_event_of_the_same_episode():
    s = _Scan([_cfg(_NET, 1)])
    t0 = _at(_SEP, 5)
    _legacy(s, _key18("JDOE", "1.2.3.4", _SEP, failed=True), t0)                       # same episode -> closed
    _legacy(s, _key18("JDOE", "1.2.3.4", _SEP), t0 + timedelta(hours=5))
    _legacy(s, _key18("AMY", "5.6.7.8", date(2026, 6, 1), failed=True), _at(date(2026, 6, 1), 5))   # old episode
    _legacy(s, _key18("AMY", "5.6.7.8", _SEP), t0 + timedelta(hours=5))
    _legacy(s, _key18("BOB", "9.9.9.9", _SEP), t0)                                     # success first: kept
    _legacy(s, _key18("BOB", "9.9.9.9", _SEP + timedelta(days=1)), t0 + timedelta(hours=20))
    _legacy(s, "OTHER_RULE|JDOE|1.2.3.4|FAILED|2026-09-30", t0)                         # another rule: kept
    _legacy(s, "OTHER_RULE|JDOE|1.2.3.4|2026-09-30", t0 + timedelta(hours=1))
    s.sweep(_SUPERSEDE, t0 + timedelta(hours=6))
    closed = {k for k, (st, kind) in s.state().items() if kind == "SUPERSEDED"}
    assert closed == {_key18("JDOE", "1.2.3.4", _SEP, failed=True)}
    # the band swaps still work
    s2 = _Scan([_cfg(_COPY, 0)])
    _legacy(s2, _key14("WARN", _D), _at(_D, 9))
    _legacy(s2, _key14("CRIT", _D), _at(_D, 10))
    s2.sweep(_SUPERSEDE, _at(_D, 10, 7))
    assert s2.state()[_key14("WARN", _D)] == ("RESOLVED", "SUPERSEDED")


# ============================================================================================================
# V091 auto-clear (R2-034): no age bound
# ============================================================================================================
_NOW = _at(date(2026, 10, 1), 10, 7)
_PERF = [_cfg("PERF_QUEUED_MINUTES", 60, auto=1), _cfg("PERF_SPILL_GB", 10, auto=1),
         _cfg("PERF_QUERY_FAIL_PCT", 10, auto=1)]


def _q(wh: str, queued_min_24h: float) -> list[dict]:
    return [{"HOUR_TS": _ms(_NOW - timedelta(hours=h)), "COMPANY": "ALFA", "WAREHOUSE_NAME": wh,
             "QUERY_COUNT": 100.0, "FAILED_COUNT": 0.0, "QUEUED_SEC_SUM": queued_min_24h * 60.0 / 4,
             "SPILL_REMOTE_GB": 0.0} for h in (1, 5, 9, 13)]


def _perf_event(wh: str, raised: datetime, status: str = "OPEN") -> dict:
    day = raised.date().isoformat()
    return {"EVENT_ID": f"p{next(_EID)}", "RULE_ID": "PERF_QUEUED_MINUTES", "COMPANY": "ALFA", "SEVERITY": "HIGH",
            "TITLE": "t", "DETAIL": None, "METRIC_VALUE": 90, "DEDUPE_KEY": f"PERF_QUEUED_MINUTES|{wh}|{day}",
            "STATUS": status, "RESOLUTION_KIND": None, "RAISED_AT": _ms(raised), "RESOLVED_AT": None}


def _clear(sql: str, events: list[dict], hourly: list[dict]) -> dict:
    s = _Scan(_PERF)
    s.events, s.hourly = [dict(e) for e in events], hourly
    s.sweep(sql, _NOW)
    return {e["EVENT_ID"]: (e["STATUS"], e["RESOLUTION_KIND"]) for e in s.events}


def test_auto_clear_reaches_events_older_than_48h():
    old = _perf_event("WH_A", _NOW - timedelta(hours=72))               # condition over since: 10 min < CLEAR 54
    held = _perf_event("WH_B", _NOW - timedelta(hours=72))              # hysteresis band: 58 min >= CLEAR 54
    days = [_perf_event("WH_C", _NOW - timedelta(days=k, hours=1)) for k in (0, 1, 2)]   # a 3-day condition ended
    ack = _perf_event("WH_A", _NOW - timedelta(hours=96), status="ACK")
    snz = _perf_event("WH_A", _NOW - timedelta(hours=97), status="SNOOZED")
    young = _perf_event("WH_C", _NOW - timedelta(minutes=30))           # inside the 1h dwell
    events = [old, held, *days, ack, snz, young]
    hourly = [*_q("WH_A", 10), *_q("WH_B", 58), *_q("WH_C", 5)]
    got = _clear(_AUTOCLEAR, events, hourly)
    assert got[old["EVENT_ID"]] == ("RESOLVED", "AUTO_CLEARED")
    assert got[held["EVENT_ID"]] == ("OPEN", None)
    assert all(got[e["EVENT_ID"]] == ("RESOLVED", "AUTO_CLEARED") for e in days)
    assert got[ack["EVENT_ID"]] == ("ACK", None) and got[snz["EVENT_ID"]] == ("SNOOZED", None)
    assert got[young["EVENT_ID"]] == ("OPEN", None)
    # teeth: V162's >= -48h bound strands the 72h event and the 2-day-old day of the condition
    got162 = _clear(_AUTOCLEAR_162, events, hourly)
    assert got162[old["EVENT_ID"]] == ("OPEN", None) and got162[days[2]["EVENT_ID"]] == ("OPEN", None)
    assert got162[days[0]["EVENT_ID"]] == ("RESOLVED", "AUTO_CLEARED")


def test_auto_clear_still_never_touches_a_rule_that_is_not_opted_in():
    ev = _perf_event("WH_A", _NOW - timedelta(hours=72))
    s = _Scan([_cfg("PERF_QUEUED_MINUTES", 60, auto=0)])
    s.events, s.hourly = [dict(ev)], _q("WH_A", 1)
    s.sweep(_AUTOCLEAR, _NOW)
    assert s.state() == {ev["DEDUPE_KEY"]: ("OPEN", None)}


def test_the_translator_fails_closed():
    with pytest.raises(AssertionError):
        _sq(_ARM14.replace("LAST_LOAD_TIME)) AS FAIL_DAY", "LAST_LOAD_TIME))::DATE AS FAIL_DAY", 1))
    with pytest.raises(AssertionError):
        _sq(_ARM18.replace("COUNT(*) AS LOGINS", "COUNT(*) + :x AS LOGINS", 1))
    with pytest.raises(AssertionError):
        _sq(_ARM14.replace("            GROUP BY 1, 2, 3, 4", "            QUALIFY 1 = 1", 1))
