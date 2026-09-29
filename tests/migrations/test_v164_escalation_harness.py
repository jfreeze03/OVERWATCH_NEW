"""Executed harness for V164's CRITICAL escalation pass (Next-Fifty #40) -- the proc's OWN statements in sqlite.

Two layers, checked against each other:
  * ``eligible`` -- a pure-Python, table-driven model of who escalates (spec 3.4): acknowledged, ACK / SNOOZED /
    RESOLVED, an incident acknowledged / mitigated / resolved, snoozed then woken, already escalated, notified
    late, never delivered (email only), HIGH, older than 7 days, the rule row missing, the email leg off, and
    ESCALATE_AFTER_MIN '0' / 'abc' / '-5' / '120.7' / '' / absent;
  * the proc's own SQL, read from the V164 migration (never a hand-written twin) and translated minimally
    (FQNs dropped, binds inlined, LEFT / LEN / CHR / IFF / TRANSLATE / DATEADD / DATEDIFF / TRY_TO_NUMBER /
    ARRAY_CONTAINS / NOW shimmed; any other unknown construct fails closed): the settings read + minutes parse,
    the capture, the per-route re-post set, the re-post LISTAGG, the ALERT_AUDIT insert and the ESCALATED_AT
    stamp. The pass's control flow (IF / FOR / the send outcomes) is driven here; SYSTEM$SEND is the one thing
    no harness can run (PART B V164.3 / V164.4 prove it live).
Clocks are Central wall-clock 'YYYY-MM-DD HH:MM:SS' (the account TIMEZONE).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import pytest

from tests._source import read

_MIG = read("snowflake/migrations/V164__notify_actionable_lines_escalation.sql")
_S = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()")
_P = _MIG[_S:_MIG.index("$$;", _MIG.index("$$", _S) + 2) + 3]
_BLOCK = _P[_P.index("    -- V164 #40: CRITICAL ESCALATION PASS"):_P.index("    -- Loud, not silent:")]
_TS = "%Y-%m-%d %H:%M:%S"
_NOW = datetime(2026, 9, 29, 10, 7)
_ALFA, _TREXIS_ROUTE = "route-alfa", "route-pager"


def _ts(d: datetime) -> str:
    return d.strftime(_TS)


def _ago(minutes: float) -> str:
    return _ts(_NOW - timedelta(minutes=minutes))


# ============================================================================================================
# The pure model (spec 3.4)
# ============================================================================================================
def try_to_number(value: str | None) -> int | None:
    """Snowflake TRY_TO_NUMBER(<varchar>) at the default NUMBER(38,0): whole number, half rounded away from 0."""
    if value is None:
        return None
    try:
        d = Decimal(value)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def after_minutes(raw: str | None) -> int:
    """COALESCE(TRY_TO_NUMBER(TRIM(COALESCE(<row value>, '120'))), 0): absent / NULL -> 120."""
    n = try_to_number(("120" if raw is None else raw).strip(" "))
    return 0 if n is None else n


@dataclass
class Ev:
    event_id: str
    raised_min_ago: float = 300
    notified_min_ago: float | None = 290
    severity: str = "CRITICAL"
    status: str = "OPEN"
    ack: bool = False
    escalated: bool = False
    rule_in_config: bool = True
    incident: tuple[bool, str] | None = None            # (incident ACK_AT set, incident STATUS)
    snoozed_ever: bool = False
    delivered_by: tuple[str, ...] = (_ALFA,)
    company: str = "ALFA"
    title: str = "Warehouse WH_X credit burn"
    detail: str | None = "Trailing 24h credits 48.2 vs threshold 25"


_ENABLED_ROUTES = (_ALFA, _TREXIS_ROUTE)


def eligible(ev: Ev, after: int, email: str = "OVERWATCH_EMAIL") -> bool:
    if after <= 0:
        return False
    first = ev.notified_min_ago if ev.notified_min_ago is not None else ev.raised_min_ago
    inc_acked = ev.incident is not None and (ev.incident[0] or ev.incident[1] != "OPEN")
    reachable = email.strip(" ") != "" or any(r in _ENABLED_ROUTES for r in ev.delivered_by)
    return (ev.severity == "CRITICAL" and ev.status == "OPEN" and not ev.ack and not ev.escalated
            and ev.rule_in_config and ev.raised_min_ago <= 7 * 24 * 60 and first >= after
            and not inc_acked and not ev.snoozed_ever and reachable)


# the table: (id, event, expected at the default 120 min with the email leg on)
_CASES = [
    ("fires", Ev("e01"), True),
    ("acked", Ev("e02", ack=True, status="ACK"), False),
    ("status ACK without ACK_AT", Ev("e03", status="ACK"), False),
    ("snoozed now", Ev("e04", status="SNOOZED", snoozed_ever=True), False),
    ("snoozed then woken", Ev("e05", snoozed_ever=True), False),
    ("resolved", Ev("e06", status="RESOLVED"), False),
    ("incident acked", Ev("e07", incident=(True, "OPEN")), False),
    ("incident mitigated", Ev("e08", incident=(False, "MITIGATED")), False),
    ("incident resolved", Ev("e09", incident=(False, "RESOLVED")), False),
    ("incident open, nobody acked", Ev("e10", incident=(False, "OPEN")), True),
    ("already escalated", Ev("e11", escalated=True), False),
    ("notified late: clock from NOTIFIED_AT", Ev("e12", raised_min_ago=400, notified_min_ago=60), False),
    ("notified 125 min ago", Ev("e13", raised_min_ago=400, notified_min_ago=125), True),
    ("never delivered: email only", Ev("e14", notified_min_ago=None, delivered_by=(), company="TREXIS"), True),
    ("never delivered, raised 60 min ago", Ev("e15", raised_min_ago=60, notified_min_ago=None, delivered_by=()),
     False),
    ("HIGH never escalates", Ev("e16", severity="HIGH"), False),
    ("older than 7 days", Ev("e17", raised_min_ago=7 * 24 * 60 + 5, notified_min_ago=7 * 24 * 60), False),
    ("the rule row is gone", Ev("e18", rule_in_config=False), False),
    ("delivered only by a disabled route", Ev("e19", delivered_by=("route-disabled",)), True),
    ("OPEN again but ACK_AT kept (acknowledged once)", Ev("e20", ack=True, status="OPEN"), False),
]


def test_model_table():
    for name, ev, want in _CASES:
        assert eligible(ev, 120) is want, name


@pytest.mark.parametrize(("raw", "want"), [
    (None, 120), ("120", 120), (" 90 ", 90), ("0", 0), ("abc", 0), ("-5", -5), ("120.7", 121), ("120.5", 121),
    ("", 0), ("15", 15)])
def test_model_minutes_parse(raw, want):
    assert after_minutes(raw) == want
    assert (after_minutes(raw) > 0) is (want > 0)


def test_model_email_leg_off_keeps_only_route_delivered_events():
    assert eligible(Ev("a"), 120, email="") is True
    assert eligible(Ev("b", delivered_by=()), 120, email="") is False
    assert eligible(Ev("c", delivered_by=("route-disabled",)), 120, email="") is False
    assert eligible(Ev("d", delivered_by=()), 120, email="  ") is False


# ============================================================================================================
# sqlite: the proc's own statements
# ============================================================================================================
def _dateadd(unit: str, n: float, x: str) -> str:
    return _ts(datetime.strptime(x, _TS) + timedelta(**{unit.lower() + "s": float(n)}))


def _datediff(unit: str, a: str, b: str) -> int:
    assert unit.lower() == "minute", unit
    ta, tb = (datetime.strptime(v, _TS).replace(second=0) for v in (a, b))
    return int((tb - ta).total_seconds() // 60)


def _translate(s: str | None, frm: str, to: str) -> str | None:
    return None if s is None else s.translate({ord(f): ord(t) for f, t in zip(frm, to, strict=True)})


def _array_contains(value: str | None, arr: str) -> int:
    return int(value in json.loads(arr))


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("SF_LEFT", 2, lambda s, n: None if s is None else s[:int(n)], deterministic=True)
    con.create_function("LEN", 1, lambda s: None if s is None else len(s), deterministic=True)
    con.create_function("CHR", 1, lambda n: chr(int(n)), deterministic=True)
    con.create_function("IFF", 3, lambda c, a, b: a if c else b, deterministic=True)
    con.create_function("TRANSLATE", 3, _translate, deterministic=True)
    con.create_function("DATEADD", 3, _dateadd, deterministic=True)
    con.create_function("DATEDIFF", 3, _datediff, deterministic=True)
    con.create_function("TRY_TO_NUMBER", 1, try_to_number, deterministic=True)
    con.create_function("ARRAY_CONTAINS", 2, _array_contains, deterministic=True)
    con.create_function("NOW_TS", 0, lambda: _ts(_NOW))
    con.executescript("""
        CREATE TABLE ALERT_EVENTS (EVENT_ID TEXT PRIMARY KEY, RULE_ID TEXT NOT NULL, RAISED_AT TEXT NOT NULL,
            COMPANY TEXT NOT NULL, SEVERITY TEXT NOT NULL, TITLE TEXT NOT NULL, DETAIL TEXT, STATUS TEXT NOT NULL,
            ACK_AT TEXT, NOTIFIED_AT TEXT, ESCALATED_AT TEXT);
        CREATE TABLE ALERT_CONFIG (RULE_ID TEXT PRIMARY KEY);
        CREATE TABLE INCIDENTS (INCIDENT_ID TEXT PRIMARY KEY, ACK_AT TEXT, STATUS TEXT NOT NULL);
        CREATE TABLE INCIDENT_MEMBERS (INCIDENT_ID TEXT, MEMBER_KIND TEXT, REF_ID TEXT);
        CREATE TABLE ALERT_AUDIT (EVENT_ID TEXT NOT NULL, ACTION TEXT NOT NULL, NOTE TEXT CHECK (length(NOTE) <= 2000),
            ACTED_BY TEXT NOT NULL CHECK (length(ACTED_BY) <= 200));
        CREATE TABLE ALERT_DELIVERIES (EVENT_ID TEXT NOT NULL, ROUTE_ID TEXT NOT NULL);
        CREATE TABLE ALERT_ROUTES (ROUTE_ID TEXT PRIMARY KEY, INTEGRATION_NAME TEXT, ENABLED INTEGER NOT NULL);
        CREATE TABLE SETTINGS (KEY TEXT PRIMARY KEY, VALUE TEXT);
        CREATE TABLE APP_ERROR_LOG (PAGE TEXT, ERROR_TYPE TEXT, ERROR_MESSAGE TEXT, CONTEXT TEXT, ROLE_NAME TEXT);
    """)
    con.executemany("INSERT INTO ALERT_ROUTES VALUES (?, ?, ?)",
                    [(_ALFA, "OVERWATCH_WEBHOOK_TEAMS", 1), (_TREXIS_ROUTE, "OVERWATCH_WEBHOOK_PAGER", 1),
                     ("route-disabled", "OVERWATCH_WEBHOOK_OLD", 0)])
    return con


def _load(con: sqlite3.Connection, events: list[Ev]) -> None:
    for ev in events:
        con.execute("INSERT INTO ALERT_EVENTS VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            ev.event_id, f"RULE_{ev.event_id}", _ago(ev.raised_min_ago), ev.company, ev.severity, ev.title,
            ev.detail, ev.status, _ago(30) if ev.ack else None,
            None if ev.notified_min_ago is None else _ago(ev.notified_min_ago), _ago(10) if ev.escalated else None))
        if ev.rule_in_config:
            con.execute("INSERT INTO ALERT_CONFIG VALUES (?)", (f"RULE_{ev.event_id}",))
        if ev.incident is not None:
            con.execute("INSERT INTO INCIDENTS VALUES (?,?,?)",
                        (f"inc-{ev.event_id}", _ago(20) if ev.incident[0] else None, ev.incident[1]))
            con.execute("INSERT INTO INCIDENT_MEMBERS VALUES (?, 'ALERT', ?)", (f"inc-{ev.event_id}", ev.event_id))
        if ev.snoozed_ever:
            con.execute("INSERT INTO ALERT_AUDIT VALUES (?, 'SNOOZE', 'x', 'OPERATOR')", (ev.event_id,))
        for r in ev.delivered_by:
            con.execute("INSERT INTO ALERT_DELIVERIES VALUES (?, ?)", (ev.event_id, r))


def _lit(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


# every "name(" the translated SQL may contain: the shimmed / sqlite-native functions, the keywords that precede a
# parenthesis, and the one INSERT column-list target. Anything else fails closed.
_NAMES_OK = {n.upper() for n in (
    "COALESCE", "MAX", "IFF", "SUM", "LEN", "REPLACE", "CHR", "TRIM", "TRANSLATE", "SF_LEFT", "DATEDIFF", "DATEADD",
    "ARRAY_CONTAINS", "TRY_TO_NUMBER", "NOW_TS", "json_group_array",
    "SELECT", "FROM", "AND", "OR", "NOT", "ON", "WHERE", "IN", "EXISTS", "OVER", "AS",
    "ALERT_AUDIT")}


def _sqlite(sql: str, binds: dict[str, object]) -> str:
    s = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("::VARIANT", "")
    s = s.replace("CURRENT_TIMESTAMP()", "NOW_TS()").replace("CURRENT_ROLE()", "'TEST_ROLE'")
    s = re.sub(r"\bLEFT\(", "SF_LEFT(", s)
    s = re.sub(r"\bARRAY_AGG\(DISTINCT ", "json_group_array(DISTINCT ", s)
    s = re.sub(r"\bUPDATE (\w+) (\w+)\n", r"UPDATE \1 AS \2\n", s)          # sqlite needs AS on an UPDATE alias
    out, last = [], 0
    for m in re.finditer(r"'(?:[^']|'')*'", s):                       # binds only outside string literals
        out.append(re.sub(r"(?<![:\w]):(\w+)", lambda b: _lit(binds[b.group(1)]), s[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(re.sub(r"(?<![:\w]):(\w+)", lambda b: _lit(binds[b.group(1)]), s[last:]))
    s = "".join(out)
    code = re.sub(r"'(?:[^']|'')*'", "''", s)
    assert "::" not in code and "WITHIN GROUP" not in code and "LISTAGG" not in code, code
    for fn in re.findall(r"\b([A-Za-z_]\w*)\s*\(", code):
        assert fn.upper() in _NAMES_OK, fn
    return s


def _stmt(start: str, end: str) -> str:
    i = _BLOCK.index(start)
    return _BLOCK[i:_BLOCK.index(end, i) + len(end)]


# the proc's statements, cut from the migration text (each anchor is asserted unique)
_SETTINGS_SQL = _stmt("        SELECT COALESCE(MAX(IFF(KEY = 'ESCALATE_AFTER_MIN'", "'ESCALATE_EMAIL_INTEGRATION');")
_PARSE_EXPR = re.search(r"esc_after := (.*);\n", _BLOCK).group(1)
_CAPTURE_SQL = _stmt("            SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP", "WHERE f.CUM_LEN <= 3000;")
_ROUTE_SET_SQL = _stmt("                    SELECT ARRAY_AGG(DISTINCT d.EVENT_ID)", ":esc_ids);")
_REPOST_SQL = _stmt("                        SELECT LISTAGG(", "(e.EVENT_ID::VARIANT, :r_esc_ids);")
_AUDIT_SQL = _stmt("                    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT", ":esc_ok);")
_STAMP_SQL = _stmt("                    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e", ":esc_ok);")
_ROUTES_SQL = _P[_P.index("    c2 CURSOR FOR"):]
_ROUTES_SQL = _ROUTES_SQL[_ROUTES_SQL.index("SELECT"):_ROUTES_SQL.index(";")]
for _anchor in (_SETTINGS_SQL, _CAPTURE_SQL, _ROUTE_SET_SQL, _REPOST_SQL, _AUDIT_SQL, _STAMP_SQL):
    assert _P.count(_anchor) == 1


def _run_settings(con: sqlite3.Connection) -> tuple[int, str]:
    sql = re.sub(r"\n\s*INTO :esc_after_s, :esc_email\n", "\n", _SETTINGS_SQL.rstrip(";"))
    raw_after, raw_email = con.execute(_sqlite(sql, {})).fetchone()
    after = con.execute(_sqlite("SELECT " + _PARSE_EXPR, {"esc_after_s": raw_after})).fetchone()[0]
    return int(after), raw_email


def _run_capture(con: sqlite3.Connection, after: int, email: str) -> list[tuple[str, int]]:
    head = "SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.RAISED_AT ASC, f.EVENT_ID)\n              INTO :esc_ids\n"
    assert _CAPTURE_SQL.lstrip().startswith(head)
    sql = "SELECT f.EVENT_ID, f.CUM_LEN\n" + _CAPTURE_SQL.lstrip()[len(head):].rstrip(";") \
        + "\nORDER BY f.RAISED_AT ASC, f.EVENT_ID"
    return con.execute(_sqlite(sql, {"esc_now": _ts(_NOW), "esc_after": after, "esc_email": email})).fetchall()


def _line_sql() -> str:
    """The ESC_LINE expression, cut from the re-post LISTAGG (the same text is at the fit and the email)."""
    inner = _REPOST_SQL[_REPOST_SQL.index("LISTAGG(") + len("LISTAGG("):_REPOST_SQL.index(", '\\n')")]
    assert _P.count(inner) == 3
    return inner


def _lines(con: sqlite3.Connection, ids: list[str]) -> list[str]:
    sql = f"SELECT {_line_sql()} FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :ids) " \
          "ORDER BY e.RAISED_AT ASC, e.EVENT_ID"
    return [r[0] for r in con.execute(_sqlite(sql, {"esc_now": _ts(_NOW), "ids": json.dumps(ids)})).fetchall()]


def _json_escape(msg: str) -> str:
    # the V064 order: backslash, quote, LF, CR (dropped), TAB
    return msg.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")


# ------------------------------------------------------------------------------------------------------------
def test_capture_matches_the_model_on_every_case():
    con = _connect()
    events = [ev for _n, ev, _w in _CASES]
    _load(con, events)
    after, email = _run_settings(con)
    assert (after, email) == (120, "OVERWATCH_EMAIL")                   # absent rows -> the proc's defaults
    got = [eid for eid, _cum in _run_capture(con, after, email)]
    want = sorted((ev for ev in events if eligible(ev, after, email)),
                  key=lambda ev: (_ago(ev.raised_min_ago), ev.event_id))
    assert got == [ev.event_id for ev in want]
    assert {"e01", "e10", "e13", "e14", "e19"} == set(got)


@pytest.mark.parametrize("raw", [None, "120", "0", "abc", "-5", "120.7", "", " 45 ", "290", "291"])
def test_settings_parse_matches_the_model(raw):
    con = _connect()
    if raw is not None:
        con.execute("INSERT INTO SETTINGS VALUES ('ESCALATE_AFTER_MIN', ?)", (raw,))
    ev = Ev("x1")                                               # first notified 290 min ago
    _load(con, [ev])
    after, email = _run_settings(con)
    assert after == after_minutes(raw)
    captured = [e for e, _c in _run_capture(con, after, email)] if after > 0 else []
    assert captured == (["x1"] if eligible(ev, after, email) else [])


def test_email_leg_off_drops_undeliverable_events_from_the_capture():
    con = _connect()
    events = [Ev("r1"), Ev("t1", delivered_by=(), company="TREXIS", notified_min_ago=None),
              Ev("d1", delivered_by=("route-disabled",))]
    _load(con, events)
    con.execute("INSERT INTO SETTINGS VALUES ('ESCALATE_EMAIL_INTEGRATION', '')")
    after, email = _run_settings(con)
    assert email == ""
    assert [e for e, _c in _run_capture(con, after, email)] == ["r1"]
    assert [ev.event_id for ev in events if eligible(ev, after, email)] == ["r1"]


def test_the_fit_equals_what_is_sent_and_a_worst_case_line_fits():
    """CUM_LEN (the proc's own 5-REPLACE fit) equals the JSON-escaped length of the '\\n'-joined lines the re-post
    sends, event for event -- so LEFT(:esc_msg, 3000) never cuts one -- including titles and details full of
    quotes, backslashes, tabs and line breaks; a NULL, empty or whitespace-only detail drops its segment."""
    nasty = 'T"it\\le\twith\nbreaks ' * 12
    events = [Ev("w1", title=nasty, detail='D"e\\tail\r\nline two\t' * 20),
              Ev("w2", raised_min_ago=299, detail=None), Ev("w3", raised_min_ago=298, detail="  "),
              Ev("w4", raised_min_ago=297, detail="\n\t\n"), Ev("w5", raised_min_ago=296, detail="\nstarts on a break")]
    con = _connect()
    _load(con, events)
    rows = _run_capture(con, 120, "OVERWATCH_EMAIL")
    ids = [e for e, _c in rows]
    assert ids == ["w1", "w2", "w3", "w4", "w5"]
    lines = _lines(con, ids)
    for k in range(1, len(ids) + 1):
        assert len(_json_escape("\n".join(lines[:k]))) == rows[k - 1][1]
    w1, w2, w3, w4, w5 = lines
    assert w1.startswith("ESCALATED (unacked 290 min) [CRITICAL] ") and w1.endswith(" | event w1")
    assert "\n" not in w1.split(" | ALFA | ", 1)[1] and "\t" not in w1.split(" | ALFA | ", 1)[1]   # detail flattened
    assert len(w1.split(" | ALFA | ", 1)[1].rsplit(" | event ", 1)[0]) == 100
    for ln, eid in ((w2, "w2"), (w3, "w3"), (w4, "w4")):
        assert ln.endswith(f"[CRITICAL] Warehouse WH_X credit burn | ALFA | event {eid}"), ln   # no empty segment
    assert " | ALFA | starts on a break | event w5" in w5                     # flattened, then trimmed


def test_worst_case_line_from_the_column_widths_fits_one_batch():
    """V004 widths: SEVERITY 20, TITLE 300 (LEFT 140), COMPANY 40, DETAIL 2000 (LEFT 100 after flattening),
    EVENT_ID 80. Every character doubling under the JSON escape, plus the 'ESCALATED (unacked N min) ' prefix
    for a 7-day-old event, stays far under the 3000-char batch, so a batch always holds at least one event."""
    esc = 2 * (1 + 20 + 2 + 140 + 3 + 40 + 3 + 100 + 9 + 80)
    prefix = len("ESCALATED (unacked 10080 min) ")
    assert esc + prefix < 900 < 3000


# ------------------------------------------------------------------------------------------------------------
# The pass: control flow driven here, every statement the proc's own.
# ------------------------------------------------------------------------------------------------------------
@dataclass
class Outcome:
    escalated: int
    reposts: dict[str, list[str]] = field(default_factory=dict)
    emailed: list[str] | None = None


def _pass(con: sqlite3.Connection, teams_ok: bool = True, email_ok: bool = True) -> Outcome:
    after, email = _run_settings(con)
    assert after > 0
    esc_ids = [e for e, _c in _run_capture(con, after, email)]
    out = Outcome(0)
    if not esc_ids:
        return out
    b = {"esc_now": _ts(_NOW), "esc_after": after, "esc_ids": json.dumps(esc_ids)}
    esc_ok: list[str] = []
    routes = 0
    for route_id, _integ in con.execute(_sqlite(_ROUTES_SQL, {})).fetchall():
        r_esc = json.loads(con.execute(_sqlite(re.sub(r"\n\s*INTO :r_esc_ids\n", "\n", _ROUTE_SET_SQL).rstrip(";"),
                                               {**b, "r_route_id": route_id})).fetchone()[0])
        if r_esc:
            msg = "\n".join(_lines(con, r_esc))
            assert len(_json_escape(msg)) <= 3000
            if teams_ok:
                out.reposts[route_id] = sorted(r_esc)
                esc_ok += r_esc
                routes += 1
    if email.strip(" ") and email_ok:                  # the proc's IF (COALESCE(TRIM(:esc_email), '') <> '')
        out.emailed = list(esc_ids)
        esc_ok += esc_ids
    if esc_ok:
        b2 = {**b, "esc_ok": json.dumps(esc_ok), "esc_routes": routes, "esc_emailed": out.emailed is not None}
        con.execute(_sqlite(_AUDIT_SQL.rstrip(";"), b2))
        out.escalated = con.execute(_sqlite(_STAMP_SQL.rstrip(";"), b2)).rowcount
    return out


def _world() -> sqlite3.Connection:
    con = _connect()
    _load(con, [Ev("a1"), Ev("a2", raised_min_ago=299, delivered_by=(_ALFA, _TREXIS_ROUTE)),
                Ev("t1", raised_min_ago=298, delivered_by=(), notified_min_ago=None, company="TREXIS"),
                Ev("h1", severity="HIGH")])
    return con


def test_pass_reposts_only_to_routes_that_delivered_and_emails_everything():
    con = _world()
    out = _pass(con)
    assert out.reposts == {_ALFA: ["a1", "a2"], _TREXIS_ROUTE: ["a2"]}      # t1 reached no route: never re-posted
    assert out.emailed == ["a1", "a2", "t1"] and out.escalated == 3
    audit = con.execute("SELECT EVENT_ID, ACTION, NOTE, ACTED_BY FROM ALERT_AUDIT WHERE ACTION = 'ESCALATE' "
                        "ORDER BY EVENT_ID").fetchall()
    assert [a[0] for a in audit] == ["a1", "a2", "t1"]
    assert all(a[3] == "SP_NOTIFY_WEBHOOK" and a[2] == "unacknowledged 120+ min; this run re-posted to 2 route(s), "
               "email sent" for a in audit)
    stamped = {r[0] for r in con.execute("SELECT EVENT_ID FROM ALERT_EVENTS WHERE ESCALATED_AT IS NOT NULL")}
    assert stamped == {"a1", "a2", "t1"}
    # once per event: the next run captures nothing, sends nothing, audits nothing
    again = _pass(con)
    assert again.escalated == 0 and not again.reposts and again.emailed is None
    assert con.execute("SELECT COUNT(*) FROM ALERT_AUDIT WHERE ACTION = 'ESCALATE'").fetchone()[0] == 3


def test_pass_email_fails_teams_ok_stamps_only_what_teams_took():
    con = _world()
    out = _pass(con, email_ok=False)
    assert out.escalated == 2 and out.emailed is None
    assert {r[0] for r in con.execute("SELECT EVENT_ID FROM ALERT_EVENTS WHERE ESCALATED_AT IS NOT NULL")} == {"a1", "a2"}
    retry = _pass(con)                                   # t1 (email only) retries on the next run
    assert retry.escalated == 1 and retry.emailed == ["t1"] and not retry.reposts


def test_pass_teams_fails_email_ok_stamps_everything_once():
    con = _world()
    out = _pass(con, teams_ok=False)
    assert out.escalated == 3 and not out.reposts


def test_pass_both_fail_stamps_nothing_and_retries():
    con = _world()
    out = _pass(con, teams_ok=False, email_ok=False)
    assert out.escalated == 0
    assert con.execute("SELECT COUNT(*) FROM ALERT_EVENTS WHERE ESCALATED_AT IS NOT NULL").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM ALERT_AUDIT WHERE ACTION = 'ESCALATE'").fetchone()[0] == 0
    assert _pass(con).escalated == 3


def test_harness_translation_fails_closed():
    with pytest.raises(AssertionError):
        _sqlite("SELECT LISTAGG(x, ',') WITHIN GROUP (ORDER BY x) FROM t", {})
    with pytest.raises(AssertionError):
        _sqlite("SELECT REGEXP_SUBSTR(x, 'a') FROM t", {})
    with pytest.raises(KeyError):
        _sqlite("SELECT :unbound", {})
