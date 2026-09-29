"""Executed harness for V162 -- SEC_LOGIN_TAKEOVER [26], SEC_ADMIN_GRANT [27] and the autodeclare exclusion.

Every statement runs from the V162 migration's OWN text (never a hand-written twin) in an in-memory sqlite, so a
window, band, key or polarity edit made in outputs/gen_v162.py -- which the byte-identity test happily follows --
still fails here. The translation is minimal and fails closed (FQNs dropped; CONVERT_TIMEZONE to Central is the
identity because every fixture clock is Central wall-clock; CONVERT_TIMEZONE to UTC goes through zoneinfo; the
derived-column-list shape becomes a CTE; any '::' cast or QUALIFY left over raises).

Clocks are integer milliseconds of Central wall-clock time since 1970-01-01, so DATEADD / comparisons are exact
and the key's '.FF3' is testable. ALERT_EVENTS' VARCHAR widths (V004) are CHECK constraints, so an over-long key,
TITLE or DETAIL fails here as it would in Snowflake.

Covers (spec 3.4): the takeover band / window / episode / dedupe matrix, the admin-grant matrix, the WARN -> CRIT
supersede through the V067 sweep text, the SP_INCIDENT_AUTODECLARE crit CTE and [attach] arm, the global toggle,
containment in the app's Account-takeover lens (security_sql.login_takeover_candidates, executed), and the
PREFLIGHT P162.1 / P162.2 grids agreeing with what the arms raise.
"""

from __future__ import annotations

import itertools
import math
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.data import security_sql
from tests._source import ROOT, read
from tests.migrations.test_v157_alert_scan_self_watch_idle_push import _derived_to_cte
from tests.migrations.test_v160_sleep_polling_alert import _code, _split

_MIG = read("snowflake/migrations/V162__security_takeover_admin_grant.sql")
_TAKE, _GRANT = "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT"
_ROLES = ("ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN", "SNOW_ACCOUNTADMINS",
          "SNOW_SYSADMINS")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_H = _proc(_MIG, "SP_ALERT_SCAN()")
_A = _proc(_MIG, "SP_INCIDENT_AUTODECLARE()")
_ARM26 = _between(_H, "    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
_ARM27 = _between(_H, "    -- [27] SEC_ADMIN_GRANT", "    IF (MOD(ct_hour, 3) = 2) THEN")
(_SUPERSEDE,) = re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS lo\n.*?;\n", _H, re.S)

# -- clocks ---------------------------------------------------------------------------------------------------
_EPOCH = datetime(1970, 1, 1)
_CT, _UTC = ZoneInfo("America/Chicago"), ZoneInfo("UTC")
_UNIT_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


def _ms(d: datetime) -> int:
    return round((d - _EPOCH).total_seconds() * 1000)


def _dtm(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=int(ms))


def _utc_ms(ms):
    """Central wall clock -> UTC wall clock (an ambiguous fall-back time reads as its first, CDT, occurrence)."""
    if ms is None:
        return None
    return _ms(_dtm(ms).replace(tzinfo=_CT).astimezone(_UTC).replace(tzinfo=None))


def _to_varchar(x, fmt=None):
    if x is None or isinstance(x, str):
        return x
    if fmt is None:
        return str(x)
    d = _dtm(x)
    if fmt == "YYYY-MM-DD HH24:MI":
        return d.strftime("%Y-%m-%d %H:%M")
    if fmt == "YYYY-MM-DD HH24:MI:SS.FF3":
        return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"
    raise AssertionError(f"unhandled TO_VARCHAR format {fmt!r}")          # fail closed


def _nullsafe(fn):
    return lambda *a: None if any(v is None for v in a) else fn(*a)


class _CountIf:
    def __init__(self) -> None:
        self.n = 0

    def step(self, v) -> None:
        self.n += 1 if v else 0

    def finalize(self) -> int:
        return self.n


class _By:
    """MAX_BY / MIN_BY(value, key)."""

    def __init__(self, pick) -> None:
        self.pick, self.best = pick, None

    def step(self, value, key) -> None:
        if key is not None and (self.best is None or self.pick(key, self.best[0])):
            self.best = (key, value)

    def finalize(self):
        return None if self.best is None else self.best[1]


class _MaxBy(_By):
    def __init__(self) -> None:
        super().__init__(lambda k, b: k > b)


class _MinBy(_By):
    def __init__(self) -> None:
        super().__init__(lambda k, b: k < b)


_SCHEMAS = {
    "ALERT_CONFIG": "RULE_ID, SEVERITY, ENABLED, THRESHOLD_NUM, CLEAR_THRESHOLD_NUM, AUTO_CLEAR_ENABLED",
    # V004 widths as CHECKs: Snowflake rejects (never truncates) a longer string
    "ALERT_EVENTS": ("EVENT_ID, RULE_ID, COMPANY, SEVERITY, TITLE CHECK (length(TITLE) <= 300), "
                     "DETAIL CHECK (length(DETAIL) <= 2000), METRIC_VALUE, "
                     "DEDUPE_KEY CHECK (length(DEDUPE_KEY) <= 300), STATUS, RESOLUTION_KIND, RAISED_AT, RESOLVED_AT"),
    "LOGIN_HISTORY": ("USER_NAME, EVENT_ID, EVENT_TIMESTAMP, IS_SUCCESS, CLIENT_IP, REPORTED_CLIENT_TYPE, "
                      "FIRST_AUTHENTICATION_FACTOR, ERROR_MESSAGE"),
    "GRANTS_TO_USERS": "GRANTEE_NAME, ROLE, CREATED_ON, DELETED_ON, GRANTED_BY",
    "INCIDENTS": "INCIDENT_ID, STATUS, COMPANY, DETECTED_AT",
    "INCIDENT_MEMBERS": "INCIDENT_ID, MEMBER_KIND, REF_ID",
    "SETTINGS": "KEY, VALUE",
}


def _connect(tables: dict, now: int) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    funcs = {
        "NOW_TS": (0, lambda: now),
        "DATEADD": (3, _nullsafe(lambda u, n, x: x + int(n) * _UNIT_MS[str(u).lower()])),
        "DATEDIFF": (3, _nullsafe(lambda u, a, b: b // _UNIT_MS[str(u).lower()] - a // _UNIT_MS[str(u).lower()])),
        "HOUR": (1, _nullsafe(lambda x: _dtm(x).hour)),
        "DAYOFWEEKISO": (1, _nullsafe(lambda x: _dtm(x).isoweekday())),
        "TO_DATE": (1, _nullsafe(lambda x: _dtm(x).date().isoformat())),
        "TO_UTC": (1, _utc_ms),
        "CEIL": (1, _nullsafe(lambda x: math.ceil(x))),
        "GREATEST": (-1, _nullsafe(lambda *a: max(a))),
        "LEAST": (-1, _nullsafe(lambda *a: min(a))),
        "IFF": (3, lambda c, a, b: a if c else b),
        "LEFT": (2, _nullsafe(lambda s, n: str(s)[:int(n)])),
        "TO_VARCHAR": (-1, _to_varchar),
        "SPLIT_PART": (3, _nullsafe(lambda s, d, n: (str(s).split(d) + [""] * int(n))[int(n) - 1])),
        "UUID_STRING": (0, lambda: str(uuid.uuid4())),
    }
    for name, (narg, fn) in funcs.items():
        con.create_function(name, narg, fn)
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    con.create_aggregate("MAX_BY", 2, _MaxBy)
    con.create_aggregate("MIN_BY", 2, _MinBy)
    for table, cols in _SCHEMAS.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
        names = [c.split()[0] for c in cols.split(", ")]
        for row in tables.get(table, ()):
            assert set(row) <= set(names), (table, set(row) - set(names))
            con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))
    return con


_STR_RE = re.compile(r"'(?:[^']|'')*'")


def _sq(sql: str) -> str:
    """Snowflake -> sqlite, minimal and fail-closed."""
    s = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    s = re.sub(r"CONVERT_TIMEZONE\('UTC', ([\w.]+)\)::TIMESTAMP_NTZ", r"TO_UTC(\1)", s)
    s = re.sub(r"CONVERT_TIMEZONE\('America/Chicago', ([\w.]+)\)::TIMESTAMP_NTZ", r"\1", s)
    s = s.replace("CURRENT_TIMESTAMP()", "NOW_TS()")
    s = re.sub(r"\bUPDATE (\w+) (\w+)\n", r"UPDATE \1 AS \2\n", s)
    if sqlite3.sqlite_version_info < (3, 39):          # HAVING without GROUP BY (k CTE): WHERE pins one RULE_ID
        s = s.replace("\n            HAVING COUNT(*) > 0\n", "\n            GROUP BY RULE_ID HAVING COUNT(*) > 0\n")
    s = _derived_to_cte(s)
    bare = _STR_RE.sub("''", s)
    assert "::" not in bare and "QUALIFY" not in bare and not re.search(r"(?<![:\w]):\w", bare), s
    return s


def _arm_stmt(block: str) -> str:
    return block[block.index("        WITH cfg AS ("):block.index(";\n    EXCEPTION")]


_EVENT_COLS = ("RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE", "DEDUPE_KEY")


def _rows(con: sqlite3.Connection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _cfg(rule: str, threshold, enabled: int = 1, severity: str = "HIGH") -> dict:
    return {"RULE_ID": rule, "SEVERITY": severity, "ENABLED": enabled, "THRESHOLD_NUM": threshold,
            "CLEAR_THRESHOLD_NUM": None, "AUTO_CLEAR_ENABLED": 0}


_CFG = [_cfg(_TAKE, 5), _cfg(_GRANT, 0)]


def _run(arm: str, now: datetime, *, logins=(), grants=(), events=(), cfg=None) -> list[dict]:
    con = _connect({"ALERT_CONFIG": _CFG if cfg is None else cfg, "LOGIN_HISTORY": list(logins),
                    "GRANTS_TO_USERS": list(grants), "ALERT_EVENTS": list(events)}, _ms(now))
    rows = _rows(con, _sq(_arm_stmt(arm)))
    assert all(tuple(r) == _EVENT_COLS for r in rows)
    # the INSERT itself must fit the V004 widths (CHECK constraints)
    for i, r in enumerate(rows):
        con.execute("INSERT INTO ALERT_EVENTS (EVENT_ID, STATUS, RAISED_AT, " + ", ".join(_EVENT_COLS) + ") "
                    "VALUES (?, 'OPEN', ?, " + ", ".join("?" * len(_EVENT_COLS)) + ")",
                    (f"new{i}", _ms(now), *r.values()))
    return rows


def _raised(rows: list[dict], now: datetime, tag: str, status: str = "OPEN") -> list[dict]:
    return [dict(r, EVENT_ID=f"{tag}{i}", STATUS=status, RAISED_AT=_ms(now), RESOLUTION_KIND=None,
                 RESOLVED_AT=None) for i, r in enumerate(rows)]


# -- fixtures -------------------------------------------------------------------------------------------------
_EID = itertools.count(10_000)


def _login(user: str, at: datetime, ok: bool, ip: str = "10.0.0.9", err: str | None = None) -> dict:
    return {"USER_NAME": user, "EVENT_ID": next(_EID), "EVENT_TIMESTAMP": _ms(at), "IS_SUCCESS": "YES" if ok else "NO",
            "CLIENT_IP": ip, "REPORTED_CLIENT_TYPE": "SNOWFLAKE_UI", "FIRST_AUTHENTICATION_FACTOR": "PASSWORD",
            "ERROR_MESSAGE": None if ok else (err or "INCORRECT_USERNAME_PASSWORD")}


def _episode(user: str, success_at: datetime, n_fail: int = 5, spread_min: float = 10, gap_min: float = 20,
             success: bool = True) -> list[dict]:
    """n_fail failures spread evenly over spread_min minutes, ending gap_min minutes before success_at."""
    end = success_at - timedelta(minutes=gap_min)
    start = end - timedelta(minutes=spread_min)
    step = spread_min / (n_fail - 1) if n_fail > 1 else 0
    fails = [_login(user, start + timedelta(minutes=step * i), False, ip=f"198.51.100.{i % 3}") for i in range(n_fail)]
    return fails + ([_login(user, success_at, True)] if success else [])


def _grant(user: str, role: str, at: datetime, deleted: datetime | None = None, by: str = "SEC_OPS_BOB") -> dict:
    return {"GRANTEE_NAME": user, "ROLE": role, "CREATED_ON": _ms(at),
            "DELETED_ON": None if deleted is None else _ms(deleted), "GRANTED_BY": by}


_WED = datetime(2026, 9, 30)          # a Wednesday (CDT)
_SAT = datetime(2026, 10, 3)
_SUN = datetime(2026, 10, 4)


def _at(day: datetime, hh: int, mm: int = 0, ss: float = 0) -> datetime:
    return day + timedelta(hours=hh, minutes=mm, seconds=ss)


def _key26(user: str, band: str, success_ct: datetime) -> str:
    utc = _dtm(_utc_ms(_ms(success_ct)))
    return f"{_TAKE}|{user[:200]}|{band}|{utc:%Y-%m-%d %H:%M:%S}.{utc.microsecond // 1000:03d}"


def _takeover(success_ct: datetime, *, user: str = "JANE.DOE", grants=(), cfg=None, **ep) -> list[dict]:
    """One episode, scanned 3h after its success (inside the 24h anchor window)."""
    return _run(_ARM26, success_ct + timedelta(hours=3), logins=_episode(user, success_ct, **ep), grants=grants,
                cfg=cfg)


# -- [26] SEC_LOGIN_TAKEOVER: band by Central clock -----------------------------------------------------------
@pytest.mark.parametrize(("success", "band"), [
    (_at(_WED, 10), "WARN"),
    (_at(_WED, 22), "CRIT"), (_at(_WED, 5, 50), "CRIT"),
    (_at(_WED, 6, 10), "WARN"), (_at(_WED, 19, 50), "WARN"),
    (_at(_WED, 5, 59, 59), "CRIT"), (_at(_WED, 6), "WARN"),
    (_at(_WED, 19, 59, 59), "WARN"), (_at(_WED, 20), "CRIT"),
    (_at(_SAT, 10), "CRIT"), (_at(_SUN, 14), "CRIT"),
    (_at(datetime(2026, 10, 2), 23, 59), "CRIT"),       # Friday night
    (_at(datetime(2026, 10, 5), 6, 30), "WARN"),        # Monday morning
])
def test_takeover_band_follows_the_central_clock_of_the_success(success, band):
    (row,) = _takeover(success)
    assert row["DEDUPE_KEY"] == _key26("JANE.DOE", band, success)
    assert row["SEVERITY"] == ("CRITICAL" if band == "CRIT" else "HIGH")          # WARN = the rule severity
    assert row["COMPANY"] == "ALL" and row["METRIC_VALUE"] == 5
    assert row["TITLE"].startswith("Possible account takeover: JANE.DOE logged in 20 min after a burst of 5 failed logins")
    assert row["TITLE"].endswith(" (off-hours)") == (band == "CRIT")
    assert f"Off-hours: {'yes' if band == 'CRIT' else 'no'}." in row["DETAIL"]
    assert row["DETAIL"].startswith(f"Success {success:%Y-%m-%d %H:%M} Central from 10.0.0.9 (SNOWFLAKE_UI, PASSWORD) "
                                    "after 5 failed logins from 3 IP(s) since ")
    assert "Never auto-declares an incident: declare one by hand." in row["DETAIL"]


def test_takeover_severity_follows_an_operator_edit_outside_the_crit_band():
    (row,) = _takeover(_at(_WED, 10), cfg=[_cfg(_TAKE, 5, severity="MEDIUM")])
    assert row["SEVERITY"] == "MEDIUM" and "|WARN|" in row["DEDUPE_KEY"]
    (crit,) = _takeover(_at(_WED, 22), cfg=[_cfg(_TAKE, 5, severity="MEDIUM")])
    assert crit["SEVERITY"] == "CRITICAL"


def test_takeover_admin_role_held_at_the_success_is_critical():
    s = _at(_WED, 10)
    for role in _ROLES:
        (row,) = _takeover(s, grants=[_grant("JANE.DOE", role, s - timedelta(days=30))])
        assert row["SEVERITY"] == "CRITICAL" and row["DEDUPE_KEY"] == _key26("JANE.DOE", "CRIT", s), role
        assert row["TITLE"].endswith(f" (admin role {role})") and f"Admin roles held: {role}." in row["DETAIL"]
    # off-hours AND admin: both reasons; two roles: the first (MIN) plus a count
    (both,) = _takeover(_at(_WED, 23), grants=[_grant("JANE.DOE", "SYSADMIN", s - timedelta(days=9)),
                                              _grant("JANE.DOE", "ACCOUNTADMIN", s - timedelta(days=9))])
    assert both["TITLE"].endswith(" (off-hours, admin role ACCOUNTADMIN)")
    assert "Admin roles held: ACCOUNTADMIN +1 more." in both["DETAIL"]


@pytest.mark.parametrize("grant", [
    _grant("JANE.DOE", "ACCOUNTADMIN", _at(_WED, 10, 1)),                                 # granted after the success
    _grant("JANE.DOE", "ACCOUNTADMIN", _at(_WED, 10, 0, 1)),                              # 1 s after
    _grant("JANE.DOE", "ACCOUNTADMIN", _at(_WED, 1), deleted=_at(_WED, 9, 59, 59)),       # revoked 1 s before
    _grant("JANE.DOE", "ACCOUNTADMIN", _at(_WED, 1), deleted=_at(_WED, 10)),              # revoked in the same ms
    _grant("JANE.DOE", "PUBLIC_READER", _at(_WED, 1)),                                    # not an admin-tier role
    _grant("JOHN.ROE", "ACCOUNTADMIN", _at(_WED, 1)),                                     # someone else
])
def test_takeover_admin_evidence_must_be_held_directly_at_the_success(grant):
    (row,) = _takeover(_at(_WED, 10), grants=[grant])
    assert "|WARN|" in row["DEDUPE_KEY"] and row["SEVERITY"] == "HIGH"
    # a grant created in the same millisecond counts as held; so does one revoked after the success
    for held_grant in (_grant("JANE.DOE", "ACCOUNTADMIN", _at(_WED, 10)),
                       _grant("JANE.DOE", "SYSADMIN", _at(_WED, 1), deleted=_at(_WED, 10, 0, 0.001))):
        (held,) = _takeover(_at(_WED, 10), grants=[held_grant])
        assert "|CRIT|" in held["DEDUPE_KEY"], held_grant


@pytest.mark.parametrize(("label", "kw"), [
    ("four failures", {"n_fail": 4}),
    ("burst spread over 40 min", {"spread_min": 40}),
    ("success 70 min after the burst", {"gap_min": 70}),
    ("lockout, no success", {"success": False}),
])
def test_takeover_negative_shapes_raise_nothing(label, kw):
    assert _takeover(_at(_WED, 10), **kw) == [], label


def test_takeover_window_edges():
    s = _at(_WED, 10)
    logins = _episode("JANE.DOE", s)
    assert _run(_ARM26, s + timedelta(hours=25), logins=logins) == []                  # anchor older than 24h
    assert len(_run(_ARM26, s + timedelta(hours=23, minutes=59), logins=logins)) == 1
    # a burst spread over exactly 15 minutes still qualifies; a success exactly 60 minutes after the burst too
    assert len(_takeover(s, spread_min=15)) == 1 and len(_takeover(s, gap_min=60)) == 1
    assert _takeover(s, spread_min=15.05) == [] and _takeover(s, gap_min=60.05) == []
    # the burst only needs ITS N failures inside 15 min: a slow trickle before it does not matter
    trickle = [_login("JANE.DOE", s - timedelta(hours=2, minutes=10 * i), False) for i in range(6)]
    assert len(_run(_ARM26, s + timedelta(hours=1), logins=[*trickle, *logins])) == 1


def test_takeover_one_event_per_episode_and_a_second_episode_raises_again():
    s = _at(_WED, 10)
    two_successes = [*_episode("JANE.DOE", s), _login("JANE.DOE", s + timedelta(minutes=30), True)]
    assert len(_run(_ARM26, s + timedelta(hours=2), logins=two_successes)) == 1
    second = _episode("JANE.DOE", s + timedelta(hours=2))
    rows = _run(_ARM26, s + timedelta(hours=4), logins=[*two_successes, *second])
    assert sorted(r["DEDUPE_KEY"] for r in rows) == sorted([_key26("JANE.DOE", "WARN", s),
                                                            _key26("JANE.DOE", "WARN", s + timedelta(hours=2))])
    # two episodes 61 minutes apart are two events; 59 minutes apart is one
    near = [*_episode("JANE.DOE", s), *_episode("JANE.DOE", s + timedelta(minutes=59))]
    far = [*_episode("JANE.DOE", s), *_episode("JANE.DOE", s + timedelta(minutes=61))]
    assert len(_run(_ARM26, s + timedelta(hours=3), logins=near)) == 1
    assert len(_run(_ARM26, s + timedelta(hours=3), logins=far)) == 2


def test_takeover_rerun_and_late_reads_never_re_raise():
    s = _at(_WED, 10)
    logins = _episode("JANE.DOE", s)
    first = _run(_ARM26, s + timedelta(hours=1), logins=logins)
    assert len(first) == 1
    for h in (2, 3, 12, 23):                           # every later hourly pass inside the window
        assert _run(_ARM26, s + timedelta(hours=h), logins=logins,
                    events=_raised(first, s + timedelta(hours=1), "e")) == []
    # a resolved (or snoozed) event still blocks its key: the dedupe is on the key, any status
    assert _run(_ARM26, s + timedelta(hours=2), logins=logins,
                events=_raised(first, s + timedelta(hours=1), "e", status="RESOLVED")) == []


def test_takeover_threshold_floor_and_ceiling():
    s = _at(_WED, 10)
    assert _takeover(s, n_fail=1, cfg=[_cfg(_TAKE, 1)]) == []                        # 1 floors to 2
    assert len(_takeover(s, n_fail=2, cfg=[_cfg(_TAKE, 1)])) == 1
    assert _takeover(s, n_fail=5, cfg=[_cfg(_TAKE, 5.5)]) == []                      # 5.5 needs 6
    assert len(_takeover(s, n_fail=6, cfg=[_cfg(_TAKE, 5.5)])) == 1
    assert len(_takeover(s, n_fail=5, cfg=[_cfg(_TAKE, None)])) == 1                 # NULL = 5
    assert _takeover(s, n_fail=4, cfg=[_cfg(_TAKE, None)]) == []
    assert _takeover(s, cfg=[_cfg(_TAKE, 5, enabled=0)]) == []                       # disabled rule
    assert _takeover(s, cfg=[_cfg(_GRANT, 0)]) == []                                 # unseeded rule
    # METRIC_VALUE is the failure count in the 75 minutes up to the success (threshold units)
    (row,) = _takeover(s, n_fail=9, spread_min=14)
    assert row["METRIC_VALUE"] == 9 and "a burst of 9 failed logins" in row["TITLE"]


def test_takeover_equal_timestamps_order_the_burst_end_first():
    """seq orders a burst end before a success in the same millisecond (FALSE < TRUE), and det counts it."""
    s = _at(_WED, 10)
    fails = [_login("JANE.DOE", s - timedelta(minutes=2 * (4 - i)), False) for i in range(5)]   # the 5th at s
    (row,) = _run(_ARM26, s + timedelta(hours=1), logins=[*fails, _login("JANE.DOE", s, True)])
    assert row["METRIC_VALUE"] == 5 and "logged in 0 min after a burst of 5" in row["TITLE"]


def test_takeover_brute_force_storm_stays_one_event_per_episode():
    s = _at(_WED, 10)
    storm = [_login("JANE.DOE", s - timedelta(minutes=50) + timedelta(seconds=i), False) for i in range(400)]
    (row,) = _run(_ARM26, s + timedelta(hours=1), logins=[*storm, _login("JANE.DOE", s, True)])
    assert row["METRIC_VALUE"] == 400 and "|WARN|" in row["DEDUPE_KEY"]


def test_takeover_keys_are_utc_millisecond_and_fit_across_dst_and_long_names():
    # fall-back night 2026-11-01: 01:30 is ambiguous -> the first (CDT) occurrence, 06:30 UTC
    s = datetime(2026, 11, 1, 1, 30, 0, 123_000)
    (row,) = _takeover(s)
    assert row["DEDUPE_KEY"].endswith("|CRIT|2026-11-01 06:30:00.123")
    s2 = datetime(2026, 11, 1, 14, 0)                          # a Sunday afternoon, CST: 20:00 UTC
    (row2,) = _takeover(s2)
    assert row2["DEDUPE_KEY"].endswith("|CRIT|2026-11-01 20:00:00.000")
    # a 255-character user name: LEFT(.., 200) keeps the key inside VARCHAR(300) (the CHECK would raise)
    long_user = "U" * 255
    (lng,) = _takeover(_at(_WED, 10), user=long_user)
    assert lng["DEDUPE_KEY"] == _key26(long_user, "WARN", _at(_WED, 10)) and len(lng["DEDUPE_KEY"]) <= 300
    assert len(lng["TITLE"]) <= 300


def _pass26(events: list[dict], logins: list[dict], grants: list[dict], now: datetime, tag: str):
    """One hourly pass in SP_ALERT_SCAN order, EXECUTED: arm [26], then the V067 supersede sweep."""
    rows = _run(_ARM26, now, logins=logins, grants=grants, events=events)
    con = _connect({"ALERT_CONFIG": _CFG, "ALERT_EVENTS": [*events, *_raised(rows, now, tag)]}, _ms(now))
    con.execute(_sq(_SUPERSEDE))
    return rows, _rows(con, "SELECT * FROM ALERT_EVENTS ORDER BY rowid")


def test_takeover_late_admin_evidence_supersedes_the_warn_and_crit_never_downgrades():
    s = _at(_WED, 10)
    logins = _episode("JANE.DOE", s)
    grant = [_grant("JANE.DOE", "SECURITYADMIN", s - timedelta(days=3))]
    # hour 1: the grant row has not landed in ACCOUNT_USAGE yet -> WARN
    rows1, ev1 = _pass26([], logins, [], s + timedelta(hours=1), "a")
    assert [r["DEDUPE_KEY"] for r in rows1] == [_key26("JANE.DOE", "WARN", s)]
    # hour 2: the grant shows -> the CRIT twin is minted and the V067 sweep supersedes the OPEN WARN
    rows2, ev2 = _pass26(ev1, logins, grant, s + timedelta(hours=2), "b")
    assert [r["DEDUPE_KEY"] for r in rows2] == [_key26("JANE.DOE", "CRIT", s)]
    state = {e["DEDUPE_KEY"]: (e["STATUS"], e["RESOLUTION_KIND"]) for e in ev2}
    assert state == {_key26("JANE.DOE", "WARN", s): ("RESOLVED", "SUPERSEDED"),
                     _key26("JANE.DOE", "CRIT", s): ("OPEN", None)}
    # hour 3: the grant row vanishes again -> nothing (the WARN key exists, and a CRIT twin blocks it anyway)
    rows3, _ = _pass26(ev2, logins, [], s + timedelta(hours=3), "c")
    assert rows3 == []
    # CRIT first, then the evidence goes: the WARN key is NEW, and only the CRIT-twin guard stops it
    rows_a, ev_a = _pass26([], logins, grant, s + timedelta(hours=1), "d")
    assert [r["DEDUPE_KEY"] for r in rows_a] == [_key26("JANE.DOE", "CRIT", s)]
    rows_b, _ = _pass26(ev_a, logins, [], s + timedelta(hours=2), "e")
    assert rows_b == []
    # teeth: without the CRIT twin the same WARN would be minted
    assert [r["DEDUPE_KEY"] for r in _run(_ARM26, s + timedelta(hours=2), logins=logins)] == [
        _key26("JANE.DOE", "WARN", s)]


# -- [27] SEC_ADMIN_GRANT -------------------------------------------------------------------------------------
_NOW27 = _at(_WED, 15)


def _key27(user: str, role: str, created: datetime) -> str:
    return f"{_GRANT}|{user[:200]}|{role}|{created:%Y-%m-%d %H:%M:%S}.{created.microsecond // 1000:03d}"


def test_admin_grant_window_rerun_and_every_role():
    fresh = _grant("AL.ICE", "SYSADMIN", _NOW27 - timedelta(hours=25, minutes=59))
    edge = _grant("AL.ICE", "ORGADMIN", _NOW27 - timedelta(hours=26))
    old = _grant("AL.ICE", "USERADMIN", _NOW27 - timedelta(hours=27))
    rows = _run(_ARM27, _NOW27, grants=[fresh, edge, old])
    assert sorted(r["DEDUPE_KEY"] for r in rows) == sorted([
        _key27("AL.ICE", "SYSADMIN", _NOW27 - timedelta(hours=25, minutes=59)),
        _key27("AL.ICE", "ORGADMIN", _NOW27 - timedelta(hours=26))])
    assert {r["SEVERITY"] for r in rows} == {"HIGH"} and {r["COMPANY"] for r in rows} == {"ALL"}
    assert {r["METRIC_VALUE"] for r in rows} == {1}
    assert _run(_ARM27, _NOW27 + timedelta(hours=1), grants=[fresh, edge, old],
                events=_raised(rows, _NOW27, "g")) == []                                # re-run: 0
    for role in _ROLES:
        (r,) = _run(_ARM27, _NOW27, grants=[_grant("AL.ICE", role, _NOW27 - timedelta(hours=2))])
        assert r["TITLE"].startswith(f"Admin role {role} granted to AL.ICE"), role
    assert _run(_ARM27, _NOW27, grants=[_grant("AL.ICE", "ANALYST", _NOW27 - timedelta(hours=2))]) == []
    assert _run(_ARM27, _NOW27, grants=[fresh], cfg=[_cfg(_GRANT, 0, enabled=0)]) == []


def test_admin_grant_revoked_regrant_first_time_and_off_hours():
    g1 = _grant("AL.ICE", "ACCOUNTADMIN", _NOW27 - timedelta(days=40), deleted=_NOW27 - timedelta(days=39))
    g2 = _grant("AL.ICE", "ACCOUNTADMIN", _at(_WED, 11), deleted=_at(_WED, 12, 30))        # revoked in the window
    g3 = _grant("BO.B", "SECURITYADMIN", _at(_WED, 2, 15), by="SCIM_PROVISIONER")          # off-hours, first time
    rows = {r["DEDUPE_KEY"]: r for r in _run(_ARM27, _NOW27, grants=[g1, g2, g3])}
    assert set(rows) == {_key27("AL.ICE", "ACCOUNTADMIN", _at(_WED, 11)),
                         _key27("BO.B", "SECURITYADMIN", _at(_WED, 2, 15))}
    regrant = rows[_key27("AL.ICE", "ACCOUNTADMIN", _at(_WED, 11))]
    assert regrant["TITLE"] == "Admin role ACCOUNTADMIN granted to AL.ICE"
    assert regrant["DETAIL"].startswith("Granted 2026-09-30 11:00 Central by SEC_OPS_BOB. Re-grant: 1 earlier grant(s)")
    assert " Since revoked 2026-09-30 12:30 Central." in regrant["DETAIL"]
    first = rows[_key27("BO.B", "SECURITYADMIN", _at(_WED, 2, 15))]
    assert first["TITLE"] == "Admin role SECURITYADMIN granted to BO.B (off-hours) - first time"
    assert "by SCIM_PROVISIONER. First grant of this role to this user on record. Still held." in first["DETAIL"]
    # a Saturday afternoon grant is off-hours; a weekday 19:59:59 is not, 20:00 is
    for at, off in ((_at(_SAT, 14), True), (_at(_WED, 19, 59, 59), False), (_at(_WED, 20), True)):
        (r,) = _run(_ARM27, at + timedelta(hours=1), grants=[_grant("CY.D", "SYSADMIN", at)])
        assert r["TITLE"].endswith(" (off-hours) - first time") is off, at


def test_admin_grant_long_names_fit():
    (r,) = _run(_ARM27, _NOW27, grants=[_grant("G" * 255, "SNOW_ACCOUNTADMINS", _NOW27 - timedelta(hours=1))])
    assert len(r["DEDUPE_KEY"]) == len(_GRANT) + 1 + 200 + 1 + len("SNOW_ACCOUNTADMINS") + 1 + 23 <= 300
    assert len(r["TITLE"]) <= 300


# -- SP_INCIDENT_AUTODECLARE ----------------------------------------------------------------------------------
_DECL = _A[_A.index("    WITH crit AS ("):_A.index("    GROUP BY FAMILY, COMPANY;") + len("    GROUP BY FAMILY, COMPANY")]
_ATTACH = _between(_A, "        SELECT t.INCIDENT_ID, 'ALERT', t.EVENT_ID", ";\n        attached := SQLROWCOUNT;")


def _crit(eid: str, rule: str, company: str = "ALL", severity: str = "CRITICAL", status: str = "OPEN",
          raised: datetime | None = None) -> dict:
    return {"EVENT_ID": eid, "RULE_ID": rule, "COMPANY": company, "SEVERITY": severity, "TITLE": f"{rule} title",
            "DEDUPE_KEY": f"{rule}|x|{eid}", "STATUS": status, "RAISED_AT": _ms(raised or _at(_WED, 9))}


def test_autodeclare_crit_cte_skips_the_two_identity_rules():
    events = [_crit("t1", _TAKE), _crit("g1", _GRANT), _crit("c1", "SEC_CRED_EXPIRY"),
              _crit("t2", _TAKE, status="ACK"), _crit("p1", "PIPE_ETL_CYCLE_LATE", company="ALFA")]
    con = _connect({"ALERT_EVENTS": events}, _ms(_at(_WED, 10)))
    got = {(r["FAMILY"], r["COMPANY"]) for r in _rows(con, _sq(_DECL))}
    assert got == {("SEC_CRED_EXPIRY", "ALL"), ("PIPE_ETL_CYCLE_LATE", "ALFA")}
    # teeth: the same text without the V162 predicate declares the takeover / grant families
    base = _DECL.replace("          AND e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')\n", "")
    assert base != _DECL
    got_base = {r["FAMILY"] for r in _rows(con, _sq(base))}
    assert {_TAKE, _GRANT} <= got_base


def _attach_sql() -> str:
    """[attach]'s SELECT with its QUALIFY moved into a ROW_NUMBER subquery (sqlite has no QUALIFY)."""
    sql = _ATTACH
    m = re.search(r"\n\s*QUALIFY (ROW_NUMBER\(\) OVER \(.*?\)) = 1\n", sql, re.S)
    assert m
    sql = sql[:m.start()] + "\n            ) WHERE QRN_ = 1\n" + sql[m.end():]
    head = "            SELECT e.EVENT_ID, e.RAISED_AT, i.INCIDENT_ID\n"
    assert sql.count(head) == 1
    return sql.replace(head, f"            SELECT * FROM (SELECT e.EVENT_ID, e.RAISED_AT, i.INCIDENT_ID, {m[1]} AS QRN_\n")


def test_autodeclare_attach_still_links_a_takeover_to_a_human_declared_incident():
    human = {"INCIDENT_ID": "INC-1", "STATUS": "OPEN", "COMPANY": "ALL", "DETECTED_AT": _ms(_at(_WED, 8))}
    old = _crit("t0", _TAKE, raised=_at(_WED, 7))
    new = _crit("t1", _TAKE, raised=_at(_WED, 9))
    con = _connect({"ALERT_EVENTS": [old, new], "INCIDENTS": [human],
                    "INCIDENT_MEMBERS": [{"INCIDENT_ID": "INC-1", "MEMBER_KIND": "ALERT", "REF_ID": "t0"}]},
                   _ms(_at(_WED, 10)))
    rows = _rows(con, _sq(_attach_sql()))
    assert [(r["INCIDENT_ID"], r["EVENT_ID"]) for r in rows] == [("INC-1", "t1")]
    # and the declare itself still makes nothing for that family
    assert _rows(con, _sq(_DECL)) == []


def test_autodeclare_toggle_off_returns_before_any_declare():
    toggle = _between(_A, "    SELECT COALESCE(MAX(VALUE), 'TRUE') INTO :enabled", "    CREATE OR REPLACE TEMPORARY TABLE")
    assert "    IF (UPPER(:enabled) <> 'TRUE') THEN\n        RETURN 'auto-declare off';\n    END IF;\n" in toggle
    read_sql = toggle[:toggle.index(";")].replace(" INTO :enabled", "")
    for value, off in (("FALSE", True), ("false", True), ("TRUE", False), (None, False)):
        con = _connect({"SETTINGS": [] if value is None else
                        [{"KEY": "INCIDENT_AUTO_DECLARE_CRITICAL", "VALUE": value}]}, 0)
        (enabled,) = con.execute(_sq(read_sql)).fetchone()
        assert (str(enabled).upper() != "TRUE") is off, value
    assert _A.index("RETURN 'auto-declare off';") < _A.index("    WITH crit AS (")


# -- containment in the app lens + the PREFLIGHT grids ------------------------------------------------------
def _combined_logins(now: datetime) -> tuple[list[dict], list[dict]]:
    base = now - timedelta(hours=6)
    logins = [*_episode("A.WARN", base), *_episode("B.NIGHT", _at(_WED, 23)),
              *_episode("C.ADMIN", base + timedelta(minutes=5)), *_episode("D.LOCKED", base, success=False),
              *_episode("E.SLOW", base, spread_min=40), *_episode("F.TWICE", base - timedelta(hours=3)),
              *_episode("F.TWICE", base), *_episode("G.OLD", now - timedelta(hours=30))]
    grants = [_grant("C.ADMIN", "SYSADMIN", now - timedelta(days=100)),
              _grant("H.NEW", "ACCOUNTADMIN", now - timedelta(hours=3)),
              _grant("I.OLD", "ORGADMIN", now - timedelta(days=10))]
    return logins, grants


_NOW_MIX = _at(datetime(2026, 10, 1), 12)          # Thursday noon


def test_every_alert_user_is_a_breakthrough_in_the_app_lens():
    logins, grants = _combined_logins(_NOW_MIX)
    raised = _run(_ARM26, _NOW_MIX, logins=logins, grants=grants)
    users = {r["DEDUPE_KEY"].split("|")[1] for r in raised}
    assert users == {"A.WARN", "B.NIGHT", "C.ADMIN", "F.TWICE"}
    con = _connect({"LOGIN_HISTORY": logins}, _ms(_NOW_MIX))
    lens = _rows(con, _sq(security_sql.login_takeover_candidates(days=7, company="ALL", min_failures=5)))
    succeeded = {r["USER_NAME"] for r in lens if r["SUCCEEDED_AFTER"]}
    assert users <= succeeded, users - succeeded
    assert "D.LOCKED" not in succeeded                                                 # both agree on a lockout


def _preflight_statements(tmp_path) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("V162_OUT", "PREFLIGHT_OUT", "PARTB_OUT")}
    env.update(V162_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(tmp_path / "pf.sql"))
    out = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v162.py")], capture_output=True, text=True,
                         env=env, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    return _split(_code((tmp_path / "pf.sql").read_text(encoding="utf-8")))


def test_preflight_grids_preview_exactly_what_the_first_run_raises(tmp_path):
    p1, p2, p3 = _preflight_statements(tmp_path)
    logins, grants = _combined_logins(_NOW_MIX)
    con = _connect({"LOGIN_HISTORY": logins, "GRANTS_TO_USERS": grants}, _ms(_NOW_MIX))
    arm26 = {r["DEDUPE_KEY"] for r in _run(_ARM26, _NOW_MIX, logins=logins, grants=grants)}
    rows1 = _rows(con, _sq(p1))
    assert {r["DEDUPE_KEY_PREVIEW"] for r in rows1 if r["IN_FIRST_RUN_WINDOW"]} == arm26
    assert {r["USER_NAME"] for r in rows1} == {"A.WARN", "B.NIGHT", "C.ADMIN", "F.TWICE", "G.OLD"}   # 30 days
    by_user = {r["USER_NAME"]: r for r in rows1}
    assert by_user["C.ADMIN"]["CRIT_REASON"] == "admin role SYSADMIN" and by_user["B.NIGHT"]["BAND"] == "CRIT"
    assert by_user["B.NIGHT"]["CRIT_REASON"] == "off-hours"
    arm27 = {r["DEDUPE_KEY"] for r in _run(_ARM27, _NOW_MIX, grants=grants)}
    rows2 = _rows(con, _sq(p2))
    assert {r["DEDUPE_KEY_PREVIEW"] for r in rows2 if r["IN_FIRST_RUN_WINDOW"]} == arm27
    assert {r["GRANTEE_NAME"] for r in rows2} == {"H.NEW", "I.OLD"}                     # C.ADMIN's is 100 days old
    assert {r["TITLE_PREVIEW"] for r in rows2} >= {"Admin role ACCOUNTADMIN granted to H.NEW - first time"}
    # P162.3 runs against the autodeclare's own tables
    events = [_crit("t1", _TAKE, raised=_NOW_MIX - timedelta(hours=2)),
              _crit("c1", "SEC_CRED_EXPIRY", raised=_NOW_MIX - timedelta(hours=2))]
    con3 = _connect({"ALERT_EVENTS": events}, _ms(_NOW_MIX))
    got = {r["RULE_ID"]: r["AUTO_DECLARES_AFTER_V162"] for r in _rows(con3, _sq(p3))}
    assert got == {_TAKE: "never (V162)", "SEC_CRED_EXPIRY": "yes"}
