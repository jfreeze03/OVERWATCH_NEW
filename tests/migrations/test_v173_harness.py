"""Executed harness for V173 -- arm [18] SEC_NEW_ADMIN_NETWORK's rewritten dedupe guard and arm [24]
COST_IDLE_OPPORTUNITY's guarded divisions.

Arm [18]: every statement runs from the V173 migration's OWN text on tests/migrations/test_v168_harness's sqlite
fixture (shared, never copied), next to V168's text. The guard must keep exactly V168's R2-036 / R2-039 outcomes:
the V168 harness scenarios replayed on the V173 arm, a randomized differential (V168 vs V173 on the same event
store, NULL keys, V162 undated keys, look-alike keys under another rule, 1h / 47h / 49h / 200h ages), and teeth for
each of the three legs and both halves of the recent CTE. sqlite decorrelates any subquery, so it cannot show the
production failure itself: tests/test_snowflake_supported_subqueries.py is the static lock for that.

Arm [24]: tests/migrations/test_v157's idle fixture plus WH_Z, a warehouse with 14 days of queries and no metering
(the mart loader's FULL OUTER JOIN row, CREDITS_TOTAL = 0 -- the row shape behind the 2026-10-01 'Division by zero').
sqlite returns NULL for x / 0 where Snowflake raises, so the guard itself is asserted statically with
tests/test_sql_division_guards.py, and the executed runs prove the events are unchanged.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

from tests._source import ROOT, read
from tests.migrations import test_v157_alert_scan_self_watch_idle_push as v157
from tests.migrations import test_v168_harness as v168h

_V173 = read("snowflake/migrations/V173__alert_scan_supported_subquery_and_div0.sql")
_V169 = read("snowflake/migrations/V169__alert_scan_daily_windows_and_keys.sql")
_H = v168h._proc(_V173, "SP_ALERT_SCAN()")
_D = v168h._proc(_V173, "SP_ALERT_SCAN_DAILY()")
_D169 = v168h._proc(_V169, "SP_ALERT_SCAN_DAILY()")
_ARM18 = v168h._arm(_H, *v168h._A18)
_ARM18_168 = v168h._ARM18
_SUPERSEDE = v168h._update(_H, "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo\n")
_NET, _SEP, _Scan, _cfg = v168h._NET, v168h._SEP, v168h._Scan, v168h._cfg
_at, _login, _key18, _legacy, _hourly = v168h._at, v168h._login, v168h._key18, v168h._legacy, v168h._hourly

_LEG2 = ("          AND NOT EXISTS (   -- (2) this pair's V162 undated key (date and '|FAILED' stripped), last 48h\n"
         "            SELECT 1 FROM recent r\n"
         "            WHERE r.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')\n"
         "        )\n")
_LEG3 = ("          AND NOT EXISTS (   -- (3) the same exact base and outcome on another first-seen day, last 48h: "
         "R2-039\n"
         "            SELECT 1 FROM recent r\n"
         "            WHERE r.KEY_LEN = LENGTH(b.DEDUPE_KEY)\n"
         "              AND r.KEY_HEAD = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10)\n"
         "        )")
_RECENT_48H = "              AND RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())\n"
_RECENT_RULE = "            WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'\n"


def _mutate(old: str, new: str) -> str:
    assert _ARM18.count(old) == 1, old[:60]
    return _ARM18.replace(old, new)


def test_the_arm_texts_are_the_migrations():
    assert _ARM18 != _ARM18_168 and _ARM18.count("FROM recent r") == 2
    assert _LEG2 in _ARM18 and _LEG3 in _ARM18 and _RECENT_48H in _ARM18 and _RECENT_RULE in _ARM18
    assert _ARM18_168.count("OR (e.RULE_ID = b.RULE_ID") == 1 and "recent" not in _ARM18_168


# ============================================================================================================
# [18] the V168 harness scenarios, replayed on the V173 arm
# ============================================================================================================
@pytest.mark.parametrize("scenario", [
    "test_arm18_failures_only_attempts_say_so_and_carry_a_failed_key",
    "test_arm18_success_only_pair_keeps_the_v162_title_and_a_dated_key",
    "test_arm18_a_network_quiet_for_90_days_alerts_again",
    "test_arm18_ip_prefixes_never_collide",
    "test_arm18_long_names_fit_the_v004_widths",
    "test_supersede_closes_only_a_failed_event_of_the_same_episode",
])
def test_v168_harness_scenarios_hold_on_the_v173_arm(scenario, monkeypatch):
    """The V168 harness functions that read only the arm (their teeth run V162's text) pass unchanged with the
    module's _ARM18 / _SUPERSEDE swapped for V173's."""
    monkeypatch.setattr(v168h, "_ARM18", _ARM18)
    monkeypatch.setattr(v168h, "_SUPERSEDE", _SUPERSEDE)
    getattr(v168h, scenario)()


def test_a_later_success_still_raises_its_own_event_and_supersedes_the_failed_one():
    """R2-039 (b): the V168 scenario on V173's text, and its teeth on V173's leg 2: a prefix compare (the R2-036
    STARTSWITH draft) would let the failures-only key swallow the success."""
    for arm in (_ARM18, _ARM18_168):
        s = _Scan([_cfg(_NET, 1)])
        s.logins = [_login("JDOE", _at(_SEP, 3, m), False) for m in (1, 2, 3)]
        (failed,) = s.arm(arm, _at(_SEP, 5, 7))
        s.logins.append(_login("JDOE", _at(_SEP, 9), True))
        (ok,) = s.arm(arm, _at(_SEP, 10, 7))
        assert (failed["DEDUPE_KEY"], ok["DEDUPE_KEY"]) == (_key18("JDOE", "203.0.113.9", _SEP, failed=True),
                                                            _key18("JDOE", "203.0.113.9", _SEP))
        s.sweep(_SUPERSEDE, _at(_SEP, 10, 7))
        assert s.state() == {failed["DEDUPE_KEY"]: ("RESOLVED", "SUPERSEDED"), ok["DEDUPE_KEY"]: ("OPEN", None)}
        assert s.arm(arm, _at(_SEP, 11, 7)) == []
    starts = _mutate("            WHERE r.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), "
                     "'|FAILED', '')\n",
                     "            WHERE r.DEDUPE_KEY || '|' LIKE LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10) || '%'\n")
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("JDOE", _at(_SEP, 3, m), False) for m in (1, 2, 3)]
    s.arm(starts, _at(_SEP, 5, 7))
    s.logins.append(_login("JDOE", _at(_SEP, 9), True))
    assert s.arm(starts, _at(_SEP, 10, 7)) == []


def test_one_event_per_episode_across_hourly_scans_and_central_midnight():
    """R2-036 (b), (c) on V173: 24 hourly scans, one event; a late EARLIER login moving FIRST_SEEN across Central
    midnight stays one event. Teeth: without leg 3 the late row mints a second, yesterday-dated key."""
    d1 = _SEP + timedelta(days=1)

    def run(arm: str) -> list[str]:
        s = _Scan([_cfg(_NET, 1)])
        s.logins = [_login("JDOE", _at(d1, 0, 10), True)]
        raised = []
        for t in _hourly(_at(d1, 0, 30), _at(d1, 23, 30)):
            if t.hour == 2:
                s.logins.append(_login("JDOE", _at(_SEP, 23, 50), False))     # late ACCOUNT_USAGE row
            raised += [e["DEDUPE_KEY"] for e in s.arm(arm, t)]
        return raised

    assert run(_ARM18) == run(_ARM18_168) == [_key18("JDOE", "203.0.113.9", d1)]
    assert run(_mutate(_LEG3, "")) == [_key18("JDOE", "203.0.113.9", d1), _key18("JDOE", "203.0.113.9", _SEP)]


def test_a_pre_v168_undated_key_blocks_its_own_episode_only():
    """R2-036 (d): an undated V162 key raised under 48h ago blocks the same episode (success or failures-only);
    one 89 days old does not. Teeth: leg 2 without the '|FAILED' strip re-raises a failures-only episode once."""
    for arm in (_ARM18, _ARM18_168):
        for ok in (True, False):
            s = _Scan([_cfg(_NET, 1)])
            _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP, 4, 7))
            s.logins = [_login("JDOE", _at(_SEP, 3), ok)]
            assert s.arm(arm, _at(_SEP, 5, 7)) == [], ok
        s = _Scan([_cfg(_NET, 1)])
        _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP - timedelta(days=89), 4, 7))
        s.logins = [_login("JDOE", _at(_SEP, 3), True)]
        assert len(s.arm(arm, _at(_SEP, 5, 7))) == 1
    raw = _mutate("REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')",
                  "LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11)")
    s = _Scan([_cfg(_NET, 1)])
    _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP, 4, 7))
    s.logins = [_login("JDOE", _at(_SEP, 3), False)]
    assert len(s.arm(raw, _at(_SEP, 5, 7))) == 1
    # and without leg 2 at all, the undated success key no longer blocks its own episode
    s = _Scan([_cfg(_NET, 1)])
    _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP, 4, 7))
    s.logins = [_login("JDOE", _at(_SEP, 3), True)]
    assert len(s.arm(_mutate(_LEG2, ""), _at(_SEP, 5, 7))) == 1


def test_the_recent_cte_keeps_the_48h_and_rule_legs():
    """R and T of the V168 guard moved into the recent CTE. Teeth: without the 48h bound an 89-day-old event of the
    same base suppresses the 90-quiet-days re-alert (R2-036); without the rule filter an event of another rule
    whose key text equals this pair's undated key blocks it (V168 required e.RULE_ID = b.RULE_ID)."""
    jan = date(2026, 1, 15)

    def quiet(arm: str) -> list[dict]:
        s = _Scan([_cfg(_NET, 1)])
        _legacy(s, _key18("JDOE", "203.0.113.9", jan), _at(_SEP - timedelta(days=1), 9), status="RESOLVED")
        s.events[-1]["RAISED_AT"] = v168h._ms(_at(jan, 9))
        s.logins = [_login("JDOE", _at(jan, 8), True), _login("JDOE", _at(_SEP, 8), True)]
        return s.arm(arm, _at(_SEP, 9, 7))

    assert [e["DEDUPE_KEY"] for e in quiet(_ARM18)] == [_key18("JDOE", "203.0.113.9", _SEP)]
    assert quiet(_mutate(_RECENT_48H, "")) == []

    def lookalike(arm: str) -> list[dict]:
        s = _Scan([_cfg(_NET, 1)])
        _legacy(s, f"{_NET}|JDOE|203.0.113.9", _at(_SEP, 4, 7))
        s.events[-1]["RULE_ID"] = "OTHER_RULE"
        s.logins = [_login("JDOE", _at(_SEP, 3), True)]
        return s.arm(arm, _at(_SEP, 5, 7))

    assert len(lookalike(_ARM18)) == len(lookalike(_ARM18_168)) == 1
    assert lookalike(_mutate(_RECENT_RULE, "            WHERE 1 = 1\n")) == []


# ============================================================================================================
# [18] randomized differential: V173's guard keeps exactly V168's candidates
# ============================================================================================================
_USERS = ("JDOE", "first.last.name", "A|B")
_IPS = ("10.0.0.1", "10.0.0.12", "(none)", "203.0.113.9")
_DAYS = (_SEP - timedelta(days=1), _SEP, _SEP + timedelta(days=1))
_NOW18 = _at(_SEP + timedelta(days=1), 12, 7)


def _random_event(rng: random.Random, n: int) -> dict:
    u, ip = rng.choice(_USERS), rng.choice(_IPS)
    roll = rng.random()
    if roll < 0.35:
        rule, key = _NET, _key18(u, ip, rng.choice(_DAYS), failed=rng.random() < 0.5)
    elif roll < 0.55:
        rule, key = _NET, f"{_NET}|{u}|{ip}"                                     # V162 undated
    elif roll < 0.62:
        rule, key = _NET, None
    elif roll < 0.8:
        rule, key = "OTHER_RULE", rng.choice((_key18(u, ip, rng.choice(_DAYS), rng.random() < 0.5),
                                               f"{_NET}|{u}|{ip}"))               # look-alike text, other rule
    else:
        rule, key = "OTHER", f"OTHER|{u}|{rng.choice(_DAYS).isoformat()}"
    age_h = rng.choice((1, 47, 49, 200))
    return {"EVENT_ID": f"r{n}", "RULE_ID": rule, "COMPANY": "ALL", "SEVERITY": "HIGH", "TITLE": "t",
            "DETAIL": None, "METRIC_VALUE": 1, "DEDUPE_KEY": key, "STATUS": rng.choice(("OPEN", "RESOLVED")),
            "RESOLUTION_KIND": None, "RAISED_AT": v168h._ms(_NOW18 - timedelta(hours=age_h)), "RESOLVED_AT": None}


def _candidates(arm: str, s: _Scan, now: datetime = _NOW18) -> list[tuple]:
    cur = s._con(now).execute(v168h._sq(arm))
    return sorted(tuple(r) for r in cur.fetchall())


def test_randomized_differential_v168_vs_v173():
    rng = random.Random(173)
    raised = 0
    for trial in range(400):
        s = _Scan([_cfg(_NET, 1)])
        s.events = [_random_event(rng, k) for k in range(rng.randint(0, 8))]
        s.logins = [_login(rng.choice(_USERS), _NOW18 - timedelta(hours=rng.choice((2, 13, 23))),
                           rng.random() < 0.5, ip=rng.choice(_IPS))
                    for _ in range(rng.randint(1, 5))]
        s.grants.append({"GRANTEE_NAME": "A|B", "ROLE": "SNOW_SYSADMINS", "DELETED_ON": None})
        new, old = _candidates(_ARM18, s), _candidates(_ARM18_168, s)
        assert new == old, (trial, s.events, s.logins)
        raised += bool(new)
    assert 50 < raised < 400, raised                     # both outcomes occur (each leg's own teeth: below)


def test_the_differential_has_teeth():
    """Each leg matters on the random fixtures: dropping any one of them changes some trial's candidates."""
    rng = random.Random(1730)
    fixtures = []
    for _ in range(300):
        s = _Scan([_cfg(_NET, 1)])
        s.events = [_random_event(rng, j) for j in range(rng.randint(1, 8))]
        s.logins = [_login(rng.choice(_USERS[:2]), _NOW18 - timedelta(hours=rng.choice((2, 13, 23))),
                           rng.random() < 0.5, ip=rng.choice(_IPS)) for _ in range(rng.randint(1, 4))]
        fixtures.append(s)
    leg1 = ("        WHERE NOT EXISTS (   -- (1) the exact key (user|IP[|FAILED]|first-seen day), any age: R2-036\n"
            "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
            "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
            "        )\n"
            "          AND NOT EXISTS (")
    variants = {"leg 1": _mutate(leg1, "        WHERE NOT EXISTS ("), "leg 2": _mutate(_LEG2, ""),
                "leg 3": _mutate(_LEG3, ""),
                "48h": _mutate(_RECENT_48H, ""), "rule": _mutate(_RECENT_RULE, "            WHERE 1 = 1\n")}
    for name, arm in variants.items():
        assert any(_candidates(arm, s) != _candidates(_ARM18, s) for s in fixtures), name


# ============================================================================================================
# [18] PREFLIGHT: P173.2 is the arm's statement; P173.3 previews the pairs the failing arm never raised, PART B
#      V173.4 lists them once the first good scan has run
# ============================================================================================================
def _gen(tmp_path) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("V173_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(V173_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(tmp_path / "pf.sql"),
               PART_B_OUT=str(tmp_path / "pb.sql"))
    out = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v173.py")], env=env, cwd=tmp_path,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return (tmp_path / "pf.sql").read_text(encoding="utf-8"), (tmp_path / "pb.sql").read_text(encoding="utf-8")


def _grid(pf: str, head: str, nxt: str) -> str:
    g = pf[pf.index(head):pf.index(nxt)]
    body = "\n".join(ln for ln in g.splitlines() if not ln.startswith("--"))
    return body.strip().rstrip(";")


def test_preflight_p173_2_raises_what_the_arm_raises(tmp_path):
    pf, _ = _gen(tmp_path)
    grid = _grid(pf, "-- P173.2 ", "-- P173.3 ")
    s = _Scan([_cfg(_NET, 1)])
    s.logins = [_login("JDOE", _at(_SEP, 3, m), False) for m in (1, 2)] + [_login("first.last.name", _at(_SEP, 4),
                                                                                  True, ip="198.51.100.7")]
    now = _at(_SEP, 6, 7)
    want = _candidates(_ARM18, s, now)
    assert len(want) == 2
    got = sorted(tuple(r) for r in s._con(now).execute(v168h._sq(grid)).fetchall())
    assert got == want


_APPLIED_168 = _at(_SEP, 7, 0)                    # V168's apply; arm [18] fails on every run from then on


def _outage_logins() -> list[dict]:
    """Admin logins around the outage. V162's last good run is 06:07 on _SEP; LOGIN_HISTORY lands up to 2h late."""
    return [_login("JDOE", _at(_SEP - timedelta(days=30), 8), True),                       # baseline: not new
            _login("JDOE", _at(_SEP, 8), True),
            _login("JDOE", _at(_SEP - timedelta(days=1), 5), True, ip="192.0.2.55"),       # before the window
            _login("JDOE", _at(_SEP, 5), True, ip="192.0.2.44"),      # landed after 06:07: never seen by a good run
            _login("JDOE", _at(_SEP, 6), True, ip="10.9.9.9"),         # V162 raised it at 06:07
            _login("JDOE", _at(_SEP, 10), True, ip="198.51.100.7"),    # first seen while the arm failed
            _login("first.last.name", _at(_SEP + timedelta(days=2), 1), False)]   # inside the next scan's window


def _outage_scan(cfg: dict | None = None) -> _Scan:
    s = _Scan([cfg or _cfg(_NET, 1)])
    s.logins = _outage_logins()
    _legacy(s, f"{_NET}|JDOE|10.9.9.9", _at(_SEP, 6, 7))       # V162's undated key
    return s


def _run_grid(s: _Scan, grid: str, now: datetime, versions: dict[int, datetime]) -> list[dict]:
    con = s._con(now)
    con.execute("CREATE TABLE SCHEMA_VERSION (VERSION, APPLIED_AT)")
    for v, at in versions.items():
        con.execute("INSERT INTO SCHEMA_VERSION VALUES (?, ?)", (v, v168h._ms(at)))
    cur = con.execute(v168h._sq(grid))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, x, strict=True)) for x in cur.fetchall()]


def test_preflight_p173_3_lists_the_pairs_first_seen_while_the_arm_failed(tmp_path):
    pf, _ = _gen(tmp_path)
    grid = _grid(pf, "-- P173.3 ", "-- P173.4 ")
    now = _at(_SEP + timedelta(days=2), 9, 0)
    rows = {(r["USER_NAME"], r["CLIENT_IP"]): r
            for r in _run_grid(_outage_scan(), grid, now, {168: _APPLIED_168})}
    # from 24h before V168's apply: the 05:00 pair no good run saw is listed, the alerted 06:00 pair too (as alerted)
    assert set(rows) == {("JDOE", "192.0.2.44"), ("JDOE", "10.9.9.9"), ("JDOE", "198.51.100.7"),
                         ("first.last.name", "203.0.113.9")}
    assert {k: (r["NOT_ALERTED"], r["IN_ARM_WINDOW_NOW"]) for k, r in rows.items()} == {
        ("JDOE", "192.0.2.44"): (1, 0), ("JDOE", "10.9.9.9"): (0, 0), ("JDOE", "198.51.100.7"): (1, 0),
        ("first.last.name", "203.0.113.9"): (1, 1)}
    # the V173 text's old bound (V168's apply itself) lost the 05:00 pair
    old = grid.replace("DATEADD('hour', -24, (SELECT MAX(APPLIED_AT)", "((SELECT MAX(APPLIED_AT)", 1)
    assert old != grid
    assert ("JDOE", "192.0.2.44") not in {(r["USER_NAME"], r["CLIENT_IP"])
                                          for r in _run_grid(_outage_scan(), old, now, {168: _APPLIED_168})}


def test_part_b_v173_4_lists_exactly_the_pairs_the_arm_never_raises(tmp_path):
    """V173 applied at 08:30, the first good scan at 09:07 raises what is in its 24h window, V173.4 at 09:30 lists the
    rest: every pair no event covers that is past the window -- the arm's own pairs (threshold, enabled)."""
    _, pb = _gen(tmp_path)
    grid = pb[pb.index("-- V173.4 "):]
    grid = "\n".join(ln for ln in grid.splitlines() if not ln.startswith("--")).strip().rstrip(";")
    day = _SEP + timedelta(days=2)
    s = _outage_scan()
    assert [e["DEDUPE_KEY"] for e in s.arm(_ARM18, _at(day, 9, 7))] == [_key18("first.last.name", "203.0.113.9",
                                                                              day, failed=True)]
    got = [(r["USER_NAME"], r["CLIENT_IP"]) for r in _run_grid(s, grid, _at(day, 9, 30), {168: _APPLIED_168})]
    assert got == [("JDOE", "192.0.2.44"), ("JDOE", "198.51.100.7")]           # oldest first
    # what the arm would skip anyway is not listed: under the threshold, or the rule disabled
    for cfg in (_cfg(_NET, 2), _cfg(_NET, 1, enabled=0)):
        assert _run_grid(_outage_scan(cfg), grid, _at(day, 9, 30), {168: _APPLIED_168}) == []


# ============================================================================================================
# PART B V173.2 / V173.3: only a scan that STARTED after the apply counts
# ============================================================================================================
_FRESH = {"V173.2": ("ALERT_SCAN_HOURLY", "alert scan 14/14 rule blocks ok", _NET),
          "V173.3": ("ALERT_SCAN_DAILY", "alert scan daily 14/14 rule blocks ok (daily)", "COST_IDLE_OPPORTUNITY")}


def _scan_checks(grid: str, *, applied: datetime, now: datetime, beat: tuple[datetime, str],
                 errors: list[tuple[datetime, str, str]]) -> dict[str, str]:
    con = v168h._connect({}, v168h._ms(now))
    con.create_function("TO_VARCHAR", -1, lambda x, *_f: None if x is None else str(x))
    con.execute("CREATE TABLE SCHEMA_VERSION (VERSION, APPLIED_AT)")
    con.execute("INSERT INTO SCHEMA_VERSION VALUES (173, ?)", (v168h._ms(applied),))
    con.execute("CREATE TABLE SOURCE_FRESHNESS_STATE (SOURCE_NAME, LAST_LOAD_TS, STATUS)")
    for src, _status, _rule in _FRESH.values():
        con.execute("INSERT INTO SOURCE_FRESHNESS_STATE VALUES (?, ?, ?)", (src, v168h._ms(beat[0]), beat[1]))
    con.execute("CREATE TABLE APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, LOGGED_AT)")
    for at, etype, context in errors:
        con.execute("INSERT INTO APP_ERROR_LOG VALUES ('AlertScan', ?, 'x', ?, ?)", (etype, context, v168h._ms(at)))
    sql = v168h._sq(grid).replace("LISTAGG(DISTINCT CONTEXT, '; ')", "GROUP_CONCAT(DISTINCT CONTEXT)")
    return dict(con.execute(sql).fetchall())


@pytest.mark.parametrize("check", ["V173.2", "V173.3"])
def test_part_b_scan_checks_read_only_a_scan_that_started_after_the_apply(tmp_path, check):
    _, pb = _gen(tmp_path)
    grid = _grid(pb, f"-- {check} ", {"V173.2": "-- V173.3 ", "V173.3": "-- V173.4 "}[check])
    _src, ok_status, rule = _FRESH[check]
    old_status = ok_status.replace("14/14", "13/14")
    old_fail = (_at(_SEP, 10, 21), "rule_block_failed", f"rule {rule} - other rules unaffected")
    applied = _at(_SEP, 10, 20)
    # a scan that started at 10:15 runs the OLD body to the end: its failure (10:21) and its 13/14 heartbeat (10:22)
    # land after the apply. A correct apply reads WAIT, never FAIL / CHECK.
    got = _scan_checks(grid, applied=applied, now=_at(_SEP, 10, 30), beat=(_at(_SEP, 10, 22), old_status),
                       errors=[old_fail])
    assert set(got.values()) == {"OK"} | {v for v in got.values() if v.startswith("WAIT: ")}, got
    assert sum(v.startswith("WAIT: ") for v in got.values()) == 2, got
    # the old anchoring (the bare APPLIED_AT) reported that same correct apply as broken
    bare = grid.replace("DATEADD('minute', 55, ", "DATEADD('minute', 0, ").replace(
        "DATEADD('minute', 30, ", "DATEADD('minute', 0, ")
    assert bare != grid
    old = _scan_checks(bare, applied=applied, now=_at(_SEP, 10, 30), beat=(_at(_SEP, 10, 22), old_status),
                       errors=[old_fail])
    assert any(v.startswith(("FAIL", "CHECK")) for v in old.values()), old
    # the next scan (11:15-11:18, the V173 body): everything OK
    after = {"applied": applied, "now": _at(_SEP, 11, 30), "beat": (_at(_SEP, 11, 18), ok_status)}
    assert set(_scan_checks(grid, errors=[old_fail], **after).values()) == {"OK"}
    # ... and a failure of the V173 body itself is still caught
    new_fail = (_at(_SEP, 11, 16), "rule_block_failed", f"rule {rule} - other rules unaffected")
    got = _scan_checks(grid, errors=[old_fail, new_fail], **after)
    assert got[f"{check} no {rule} rule_block_failed since the apply"].startswith("FAIL"), got
    assert got[f"{check} no rule_block_failed of any rule since the apply"].startswith("CHECK"), got
    if check == "V173.2":
        sweep = (_at(_SEP, 11, 17), "supersede_sweep_failed", "V067 #40 escalation supersede - other rules unaffected")
        got = _scan_checks(grid, errors=[sweep], **after)
        assert got["V173.2 no supersede_sweep_failed since the apply"].startswith("FAIL"), got


# ============================================================================================================
# [24] COST_IDLE_OPPORTUNITY: the zero-credit warehouse (the 2026-10-01 'Division by zero' row shape)
# ============================================================================================================
_SPAN24 = ("    -- [24] COST_IDLE_OPPORTUNITY", "    -- [25] COST_SLEEP_POLLING")
_ARM24 = v168h._between(_D, *_SPAN24)
_ARM24_169 = v168h._between(_D169, *_SPAN24)
_WH_Z_DAYS = 14


def _zero_credit_tables(**kw) -> dict:
    """tests/migrations/test_v157's shared idle fixture plus WH_Z: 14 complete days with active hours and no
    metering -- CREDITS_TOTAL 0, BILLED_HOURS 0, IDLE_CREDITS 0, IDLE_PCT NULL (NULLIF(BILLED_HOURS, 0)) -- listed in
    the newest SHOW WAREHOUSES batch with a NULL timer (never suspends), so every gate of [24] except the credit
    total would let it through."""
    tables = v157._idle_tables(**kw)
    tables["MART_WAREHOUSE_EFFICIENCY_DAILY"] = tables["MART_WAREHOUSE_EFFICIENCY_DAILY"] + [
        {"WAREHOUSE_NAME": "WH_Z", "DAY": (v157._IDLE_TODAY - timedelta(days=k)).toordinal(), "CREDITS_TOTAL": 0.0,
         "IDLE_CREDITS": 0.0, "IDLE_PCT": None, "BILLED_HOURS": 0.0, "ACTIVE_HOURS": 5.0}
        for k in range(1, _WH_Z_DAYS + 1)]
    tables["WAREHOUSE_CONFIG_SNAPSHOT"] = tables["WAREHOUSE_CONFIG_SNAPSHOT"] + [
        {"WAREHOUSE_NAME": "WH_Z", "AUTO_SUSPEND": None, "SNAPSHOT_AT": v157._IDLE_BATCH}]
    return tables


def _keys(rows: list[dict]) -> dict:
    return {wh: (ev["DEDUPE_KEY"], ev["METRIC_VALUE"], ev["TITLE"]) for wh, ev in v157._by_wh(rows).items()}


def _scored(arm: str, tables: dict) -> dict:
    """The arm's own CTE chain, read at its `scored` step: {warehouse: (TOTAL_CREDITS, IDLE_PCT)}."""
    chain = arm[arm.index("        WITH cfg AS (\n"):arm.index("        SELECT b.RULE_ID")].rstrip()
    cur = v157._connect(tables, v157._IDLE_NOW).execute(
        v157._to_sqlite(chain + "\nSELECT WAREHOUSE_NAME, TOTAL_CREDITS, IDLE_PCT FROM scored", v157._IDLE_PRICE))
    return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def test_idle_arm_zero_credit_warehouse_raises_nothing_and_the_events_are_v169s():
    tables = _zero_credit_tables()
    wh_z = [r for r in tables["MART_WAREHOUSE_EFFICIENCY_DAILY"] if r["WAREHOUSE_NAME"] == "WH_Z"]
    assert len(wh_z) == _WH_Z_DAYS and sum(r["CREDITS_TOTAL"] for r in wh_z) == 0
    new = _keys(v157._run_arm(_ARM24, tables, v157._IDLE_NOW, v157._IDLE_PRICE))
    assert set(new) == {"WH_A", "WH_C", "WH_D", "WH_E"}                   # WH_Z: no event
    assert new == _keys(v157._run_arm(_ARM24_169, tables, v157._IDLE_NOW, v157._IDLE_PRICE))
    assert new == _keys(v157._run_arm(_ARM24, v157._idle_tables(), v157._IDLE_NOW, v157._IDLE_PRICE))


def test_idle_arm_projection_before_the_having_still_drops_the_zero():
    """Snowflake may compute the projection before the HAVING: run the arm with the HAVING gone (the order the
    optimizer is free to pick). WH_Z reaches `scored` with a NULL IDLE_PCT (NULLIF), 'IDLE_PCT >= 20' drops it, and
    the events are the same."""
    tables = _zero_credit_tables()
    having = "            HAVING SUM(CREDITS_TOTAL) > 0\n"
    assert _ARM24.count(having) == 1
    unfiltered = _ARM24.replace(having, "")
    assert _scored(unfiltered, tables)["WH_Z"] == (0.0, None) and "WH_Z" not in _scored(_ARM24, tables)
    assert _keys(v157._run_arm(unfiltered, tables, v157._IDLE_NOW, v157._IDLE_PRICE)) == _keys(
        v157._run_arm(_ARM24, tables, v157._IDLE_NOW, v157._IDLE_PRICE))


def test_idle_arm_zero_guard_is_static_because_sqlite_cannot_raise():
    """sqlite returns NULL for x / 0 where Snowflake raises 'Division by zero', so an executed run cannot tell the
    guarded text from the incident text. The guard is the repo-wide rule (tests/test_sql_division_guards.py):
    V173's arm has no unguarded division, V169's has exactly the two the incident exposed."""
    from tests.test_sql_division_guards import unguarded
    assert v157._connect({}, 0).execute("SELECT 1.0 / 0").fetchone() == (None,)
    assert unguarded(_ARM24) == []
    assert unguarded(_ARM24_169) == ["i.TOTAL_CREDITS", "s.COVERED_DAYS"]
    assert "ROUND(i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100, 1) AS IDLE_PCT" in _ARM24
    assert "ROUND(s.RECOVERABLE_CREDITS * :credit_price / NULLIF(s.COVERED_DAYS, 0) * 30, 2) AS MONTHLY_USD" in _ARM24


def test_preflight_p173_4_lists_the_zero_credit_warehouse_and_p173_5_raises_what_the_arm_raises(tmp_path):
    pf, _ = _gen(tmp_path)
    tables = _zero_credit_tables()
    p4 = _grid(pf, "-- P173.4 ", "-- P173.5 ")
    rows = v157._connect(tables, v157._IDLE_NOW).execute(v157._to_sqlite(p4)).fetchall()
    assert [r[0] for r in rows] == ["WH_Z"] and rows[0][1:3] == (_WH_Z_DAYS, 0.0)
    p5 = pf[pf.index("-- P173.5 "):]
    p5 = "\n".join(ln for ln in p5.splitlines() if not ln.startswith("--")).strip().rstrip(";")
    cur = v157._connect(tables, v157._IDLE_NOW).execute(v157._to_sqlite(p5))
    got = [dict(zip(v157._EVENT_COLS, r, strict=True)) for r in cur.fetchall()]
    assert _keys(got) == _keys(v157._run_arm(_ARM24, tables, v157._IDLE_NOW, v157._IDLE_PRICE))

