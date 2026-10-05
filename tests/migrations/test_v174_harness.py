"""Executed harness for V174 -- SNOW_PRI_GFR_PRD_ALFA_DSA in the admin-role lists of SP_ALERT_SCAN's arms [18]
SEC_NEW_ADMIN_NETWORK, [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT (owner access decision 2026-10-05).

Every statement runs from the V174 migration's OWN text, next to V173's, on the shared sqlite fixtures (never
copied): tests/migrations/test_v162_harness for [26] / [27] (grants with CREATED_ON / DELETED_ON / GRANTED_BY, the
Central clock, the V004 widths as CHECKs) and tests/migrations/test_v168_harness for [18]. What it proves:
  * the change -- a direct DSA grant raises SEC_ADMIN_GRANT, a DSA holder's takeover is CRITICAL at 10:00 on a
    weekday, a DSA holder's new network raises SEC_NEW_ADMIN_NETWORK; on V173's text none of them does (teeth);
  * nothing else moved -- the V162 / V168 harness scenarios pass on the V174 arms (with DSA added to the V162 role
    loop, which V173's arms then fail), and V173 / V174 raise the same rows for every other role;
  * the runbox text -- PREFLIGHT P174.1-P174.4 and PART B V174.1-V174.3 executed against the same fixtures.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from tests._source import ROOT, read
from tests.migrations import test_v162_harness as v162h
from tests.migrations import test_v168_harness as v168h

_DSA = "SNOW_PRI_GFR_PRD_ALFA_DSA"
_V174 = read("snowflake/migrations/V174__alert_scan_dsa_admin_role.sql")
_V173 = read("snowflake/migrations/V173__alert_scan_supported_subquery_and_div0.sql")
_H, _H173 = v162h._proc(_V174, "SP_ALERT_SCAN()"), v162h._proc(_V173, "SP_ALERT_SCAN()")
_S18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
_S26 = ("    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
_S27 = ("    -- [27] SEC_ADMIN_GRANT", "    IF (MOD(ct_hour, 3) = 2) THEN")
_ARM18, _ARM18_173 = v168h._arm(_H, *_S18), v168h._arm(_H173, *_S18)       # the arm's WITH ... SELECT
_ARM26, _ARM26_173 = v162h._between(_H, *_S26), v162h._between(_H173, *_S26)
_ARM27, _ARM27_173 = v162h._between(_H, *_S27), v162h._between(_H173, *_S27)
_SUPERSEDE = v168h._update(_H, "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo\n")
_NET, _SEP = v168h._NET, v168h._SEP
_WED, _NOW27 = v162h._WED, v162h._NOW27


def test_the_arm_texts_are_the_migrations():
    for new, old in ((_ARM18, _ARM18_173), (_ARM26, _ARM26_173), (_ARM27, _ARM27_173)):
        assert new != old and new.count(f"'{_DSA}'") == 1 and _DSA not in old
    assert v168h._update(_H173, "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo\n") == _SUPERSEDE


# ============================================================================================================
# [27] SEC_ADMIN_GRANT: a direct grant of DSA raises (V173: nothing)
# ============================================================================================================
def test_a_direct_dsa_grant_raises_sec_admin_grant():
    at = _NOW27 - timedelta(hours=2)                                   # Wednesday 13:00 Central: business hours
    grants = [v162h._grant("AL.ICE", _DSA, at)]
    (row,) = v162h._run(_ARM27, _NOW27, grants=grants)
    assert row["RULE_ID"] == "SEC_ADMIN_GRANT" and row["SEVERITY"] == "HIGH" and row["COMPANY"] == "ALL"
    assert row["TITLE"] == f"Admin role {_DSA} granted to AL.ICE - first time"
    assert row["DEDUPE_KEY"] == v162h._key27("AL.ICE", _DSA, at) and row["METRIC_VALUE"] == 1
    assert row["DETAIL"].startswith("Granted 2026-09-30 13:00 Central by SEC_OPS_BOB. First grant of this role")
    # teeth: V173's arm never saw it
    assert v162h._run(_ARM27_173, _NOW27, grants=grants) == []
    # raised once however many hourly reads see it; a disabled rule raises nothing
    assert v162h._run(_ARM27, _NOW27 + timedelta(hours=1), grants=grants,
                      events=v162h._raised([row], _NOW27, "g")) == []
    assert v162h._run(_ARM27, _NOW27, grants=grants, cfg=[v162h._cfg("SEC_ADMIN_GRANT", 0, enabled=0)]) == []


def test_a_dsa_grant_off_hours_revoked_or_regranted_says_so_and_fits():
    sat = v162h._at(v162h._SAT, 14)
    (off,) = v162h._run(_ARM27, sat + timedelta(hours=1), grants=[v162h._grant("CY.D", _DSA, sat)])
    assert off["TITLE"] == f"Admin role {_DSA} granted to CY.D (off-hours) - first time"
    g1 = v162h._grant("AL.ICE", _DSA, _NOW27 - timedelta(days=40), deleted=_NOW27 - timedelta(days=39))
    g2 = v162h._grant("AL.ICE", _DSA, v162h._at(_WED, 11), deleted=v162h._at(_WED, 12, 30))
    (re_,) = v162h._run(_ARM27, _NOW27, grants=[g1, g2])
    assert re_["TITLE"] == f"Admin role {_DSA} granted to AL.ICE"
    assert "Re-grant: 1 earlier grant(s)" in re_["DETAIL"] and " Since revoked 2026-09-30 12:30 Central." in re_["DETAIL"]
    # the longest role name of the list keeps a 255-character grantee's key inside VARCHAR(300)
    (lng,) = v162h._run(_ARM27, _NOW27, grants=[v162h._grant("G" * 255, _DSA, _NOW27 - timedelta(hours=1))])
    assert len(lng["DEDUPE_KEY"]) == len("SEC_ADMIN_GRANT") + 1 + 200 + 1 + len(_DSA) + 1 + 23 <= 300
    assert len(lng["TITLE"]) <= 300


# ============================================================================================================
# [26] SEC_LOGIN_TAKEOVER: a takeover of a direct DSA holder is CRITICAL in business hours (V173: the HIGH WARN band)
# ============================================================================================================
def test_a_dsa_holder_takeover_is_critical():
    s = v162h._at(_WED, 10)
    grants = [v162h._grant("DSA.USER", _DSA, s - timedelta(days=30))]
    logins = v162h._episode("DSA.USER", s)
    (row,) = v162h._run(_ARM26, s + timedelta(hours=3), logins=logins, grants=grants)
    assert row["SEVERITY"] == "CRITICAL" and row["DEDUPE_KEY"] == v162h._key26("DSA.USER", "CRIT", s)
    assert row["TITLE"].endswith(f" (admin role {_DSA})") and f"Admin roles held: {_DSA}." in row["DETAIL"]
    (old,) = v162h._run(_ARM26_173, s + timedelta(hours=3), logins=logins, grants=grants)
    assert old["SEVERITY"] == "HIGH" and old["DEDUPE_KEY"] == v162h._key26("DSA.USER", "WARN", s)
    # held directly AT the success only: granted after it, or revoked before it, is still the WARN band
    for late in (v162h._grant("DSA.USER", _DSA, s + timedelta(minutes=1)),
                 v162h._grant("DSA.USER", _DSA, s - timedelta(days=3), deleted=s - timedelta(seconds=1))):
        (warn,) = v162h._run(_ARM26, s + timedelta(hours=3), logins=logins, grants=[late])
        assert "|WARN|" in warn["DEDUPE_KEY"] and warn["SEVERITY"] == "HIGH"


def test_a_dsa_holder_with_another_admin_role_names_the_first_plus_a_count():
    s = v162h._at(_WED, 10)
    grants = [v162h._grant("DSA.USER", _DSA, s - timedelta(days=9)),
              v162h._grant("DSA.USER", "SYSADMIN", s - timedelta(days=9))]
    (row,) = v162h._run(_ARM26, s + timedelta(hours=1), logins=v162h._episode("DSA.USER", s), grants=grants)
    assert row["TITLE"].endswith(f" (admin role {_DSA})") and f"Admin roles held: {_DSA} +1 more." in row["DETAIL"]


def test_the_crit_twin_of_an_earlier_warn_supersedes_it_after_the_apply():
    """FIRST RUN: a DSA holder's episode raised as WARN before V174 re-raises as CRIT, and the V067 sweep (the V174
    text) supersedes the WARN row -- the same path as late admin evidence (V162)."""
    s = v162h._at(_WED, 10)
    logins = v162h._episode("DSA.USER", s)
    grants = [v162h._grant("DSA.USER", _DSA, s - timedelta(days=30))]
    before = v162h._run(_ARM26_173, s + timedelta(hours=1), logins=logins, grants=grants)
    events = v162h._raised(before, s + timedelta(hours=1), "w")
    rows = v162h._run(_ARM26, s + timedelta(hours=2), logins=logins, grants=grants, events=events)
    assert [r["DEDUPE_KEY"] for r in rows] == [v162h._key26("DSA.USER", "CRIT", s)]
    con = v162h._connect({"ALERT_CONFIG": v162h._CFG,
                          "ALERT_EVENTS": [*events, *v162h._raised(rows, s + timedelta(hours=2), "c")]},
                         v162h._ms(s + timedelta(hours=2)))
    con.execute(v162h._sq(_SUPERSEDE))
    state = {e["DEDUPE_KEY"]: (e["STATUS"], e["RESOLUTION_KIND"])
             for e in v162h._rows(con, "SELECT * FROM ALERT_EVENTS")}
    assert state == {v162h._key26("DSA.USER", "WARN", s): ("RESOLVED", "SUPERSEDED"),
                     v162h._key26("DSA.USER", "CRIT", s): ("OPEN", None)}


# ============================================================================================================
# [18] SEC_NEW_ADMIN_NETWORK: a DSA holder's new network raises (V173: not an admin)
# ============================================================================================================
def _dsa_scan(role: str = _DSA, deleted=None) -> v168h._Scan:
    s = v168h._Scan([v168h._cfg(_NET, 1)])
    s.grants = [{"GRANTEE_NAME": "DSA.USER", "ROLE": role, "DELETED_ON": deleted}]
    s.logins = [v168h._login("DSA.USER", v168h._at(_SEP, 3), True)]
    return s


def test_a_dsa_holder_new_network_raises_sec_new_admin_network():
    (ev,) = _dsa_scan().arm(_ARM18, v168h._at(_SEP, 5, 7))
    assert ev["DEDUPE_KEY"] == v168h._key18("DSA.USER", "203.0.113.9", _SEP)
    assert ev["TITLE"] == "DSA.USER logged in from new network 203.0.113.9"
    assert _dsa_scan().arm(_ARM18_173, v168h._at(_SEP, 5, 7)) == []                    # teeth: V173 never saw it
    # a revoked grant is no longer an admin (DELETED_ON IS NULL), on both texts; a non-admin role neither
    assert _dsa_scan(deleted=v168h._ms(v168h._at(_SEP, 1))).arm(_ARM18, v168h._at(_SEP, 5, 7)) == []
    assert _dsa_scan(role="ANALYST").arm(_ARM18, v168h._at(_SEP, 5, 7)) == []


# ============================================================================================================
# Nothing else moved: the V162 / V168 harness scenarios on the V174 arms, and V173 == V174 for every other role
# ============================================================================================================
_V162_SCENARIOS = (
    "test_takeover_admin_role_held_at_the_success_is_critical",
    "test_takeover_one_event_per_episode_and_a_second_episode_raises_again",
    "test_takeover_rerun_and_late_reads_never_re_raise",
    "test_takeover_threshold_floor_and_ceiling",
    "test_takeover_equal_timestamps_order_the_burst_end_first",
    "test_takeover_window_edges",
    "test_takeover_keys_are_utc_millisecond_and_fit_across_dst_and_long_names",
    "test_takeover_late_admin_evidence_supersedes_the_warn_and_crit_never_downgrades",
    "test_admin_grant_window_rerun_and_every_role",
    "test_admin_grant_revoked_regrant_first_time_and_off_hours",
    "test_admin_grant_long_names_fit",
)


@pytest.mark.parametrize("scenario", _V162_SCENARIOS)
def test_v162_harness_scenarios_hold_on_the_v174_arms_with_dsa_in_the_role_loop(scenario, monkeypatch):
    monkeypatch.setattr(v162h, "_ARM26", _ARM26)
    monkeypatch.setattr(v162h, "_ARM27", _ARM27)
    monkeypatch.setattr(v162h, "_ROLES", (*v162h._ROLES, _DSA))
    getattr(v162h, scenario)()


@pytest.mark.parametrize("scenario", ["test_takeover_admin_role_held_at_the_success_is_critical",
                                      "test_admin_grant_window_rerun_and_every_role"])
def test_the_role_loop_has_teeth_on_the_v173_arms(scenario, monkeypatch):
    monkeypatch.setattr(v162h, "_ARM26", _ARM26_173)
    monkeypatch.setattr(v162h, "_ARM27", _ARM27_173)
    monkeypatch.setattr(v162h, "_ROLES", (*v162h._ROLES, _DSA))
    with pytest.raises((AssertionError, ValueError)):
        getattr(v162h, scenario)()


@pytest.mark.parametrize("scenario", [
    "test_arm18_failures_only_attempts_say_so_and_carry_a_failed_key",
    "test_arm18_success_only_pair_keeps_the_v162_title_and_a_dated_key",
    "test_arm18_a_network_quiet_for_90_days_alerts_again",
    "test_arm18_ip_prefixes_never_collide",
    "test_arm18_long_names_fit_the_v004_widths",
    "test_supersede_closes_only_a_failed_event_of_the_same_episode",
])
def test_v168_harness_scenarios_hold_on_the_v174_arm(scenario, monkeypatch):
    monkeypatch.setattr(v168h, "_ARM18", _ARM18)
    monkeypatch.setattr(v168h, "_SUPERSEDE", _SUPERSEDE)
    getattr(v168h, scenario)()


@pytest.mark.parametrize("role", [*v162h._ROLES, "ANALYST", "PUBLIC", "SNOW_PRI_GFR_PRD_ALFA_DTI",
                                  "SNOW_PRI_GFR_NONPRD_ALFA_DSA"])
def test_v173_and_v174_raise_the_same_rows_for_every_other_role(role):
    s = v162h._at(_WED, 10)
    grants = [v162h._grant("AL.ICE", role, _NOW27 - timedelta(hours=2)),
              v162h._grant("JANE.DOE", role, s - timedelta(days=30))]
    logins = v162h._episode("JANE.DOE", s)
    for new, old, now in ((_ARM27, _ARM27_173, _NOW27), (_ARM26, _ARM26_173, s + timedelta(hours=3))):
        assert v162h._run(new, now, grants=grants, logins=logins) == v162h._run(old, now, grants=grants,
                                                                                 logins=logins)
    for arm in (_ARM18, _ARM18_173):
        sc = _dsa_scan(role=role)
        watched = role in ("ACCOUNTADMIN", "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS")
        assert len(sc.arm(arm, v168h._at(_SEP, 5, 7))) == int(watched), role


# ============================================================================================================
# PREFLIGHT P174.1-P174.4 and PART B V174.1-V174.3, executed
# ============================================================================================================
def _gen(tmp_path) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("V174_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(V174_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(tmp_path / "pf.sql"),
               PART_B_OUT=str(tmp_path / "pb.sql"))
    out = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v174.py")], env=env, cwd=tmp_path,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return (tmp_path / "pf.sql").read_text(encoding="utf-8"), (tmp_path / "pb.sql").read_text(encoding="utf-8")


def _grid(text: str, head: str, nxt: str | None) -> str:
    g = text[text.index(head):text.index(nxt) if nxt else len(text)]
    return "\n".join(ln for ln in g.splitlines() if not ln.startswith("--")).strip().rstrip(";")


def _p1(con: sqlite3.Connection, grid: str) -> dict:
    sql = v162h._sq(grid).replace("LISTAGG(DISTINCT ROLE, ', ')", "GROUP_CONCAT(DISTINCT ROLE)")
    return {r["USER_NAME"]: r for r in v162h._rows(con, sql)}


def test_preflight_p174_1_lists_the_direct_holders_and_what_is_new(tmp_path):
    pf, _ = _gen(tmp_path)
    grid = _grid(pf, "-- P174.1 ", "-- P174.2 ")
    old = v162h._at(_WED, 1) - timedelta(days=200)
    grants = [v162h._grant("ONLY.DSA", _DSA, old, by="IDP"),
              v162h._grant("DSA.AND.SYS", _DSA, old), v162h._grant("DSA.AND.SYS", "SYSADMIN", old),
              v162h._grant("DSA.AND.SNOW", _DSA, old), v162h._grant("DSA.AND.SNOW", "SNOW_SYSADMINS", old),
              v162h._grant("GONE", _DSA, old, deleted=old + timedelta(days=1)),        # revoked: not a holder
              v162h._grant("SYS.ONLY", "SYSADMIN", old)]                               # not a DSA holder
    con = v162h._connect({"GRANTS_TO_USERS": grants}, v162h._ms(_NOW27))
    rows = _p1(con, grid)
    assert set(rows) == {"ONLY.DSA", "DSA.AND.SYS", "DSA.AND.SNOW"}
    assert {u: (r["NEW_TO_ARM_18"], r["NEW_TO_ARM_26"], r["OTHER_ADMIN_ROLES"]) for u, r in rows.items()} == {
        "ONLY.DSA": (1, 1, None), "DSA.AND.SYS": (1, 0, "SYSADMIN"), "DSA.AND.SNOW": (0, 0, "SNOW_SYSADMINS")}
    assert rows["ONLY.DSA"]["LAST_GRANTED_BY"] == "IDP"


@pytest.mark.parametrize(("head", "nxt", "arm"), [("-- P174.2 ", "-- P174.3 ", "27"),
                                                  ("-- P174.3 ", "-- P174.4 ", "26")])
def test_preflight_p174_2_and_3_raise_what_the_arms_raise(tmp_path, head, nxt, arm):
    pf, _ = _gen(tmp_path)
    grid = _grid(pf, head, nxt)
    s = v162h._at(_WED, 10)
    grants = [v162h._grant("AL.ICE", _DSA, _NOW27 - timedelta(hours=2)),
              v162h._grant("CY.D", "SYSADMIN", _NOW27 - timedelta(hours=3)),
              v162h._grant("DSA.USER", _DSA, s - timedelta(days=30))]
    logins = v162h._episode("DSA.USER", s)
    now = _NOW27 if arm == "27" else s + timedelta(hours=3)
    want = v162h._run(_ARM27 if arm == "27" else _ARM26, now, grants=grants, logins=logins)
    assert len(want) == (2 if arm == "27" else 1)
    con = v162h._connect({"ALERT_CONFIG": v162h._CFG, "GRANTS_TO_USERS": grants, "LOGIN_HISTORY": logins},
                         v162h._ms(now))
    def key(r: dict) -> str:
        return r["DEDUPE_KEY"]

    assert sorted(v162h._rows(con, v162h._sq(grid)), key=key) == sorted(want, key=key)


def test_preflight_p174_4_raises_what_arm_18_raises(tmp_path):
    pf, _ = _gen(tmp_path)
    grid = _grid(pf, "-- P174.4 ", None)
    s = _dsa_scan()
    now = v168h._at(_SEP, 5, 7)
    got = s._con(now).execute(v168h._sq(grid)).fetchall()
    (want,) = s.arm(_ARM18, now)
    assert [tuple(r) for r in got] == [tuple(want.values())]


def _part_b_con(now: datetime, *, applied: datetime | None, body: str, name: str | None) -> sqlite3.Connection:
    con = v162h._connect({}, v162h._ms(now))
    # (the translation drops the DBA_MAINT_DB.OVERWATCH. prefix, inside the GET_DDL literal too)
    con.create_function("GET_DDL", 2, lambda kind, obj: body if (kind, obj) == ("PROCEDURE", "SP_ALERT_SCAN()")
                        else None)
    con.create_function("CONTAINS", 2, lambda a, b: None if a is None or b is None else int(b in a))
    con.create_function("REGEXP_COUNT", 2, lambda a, p: None if a is None else len(re.findall(p, a)))
    con.create_function("TO_VARCHAR", -1, lambda x, *_f: None if x is None else str(x))
    con.execute("CREATE TABLE SCHEMA_VERSION (VERSION, APPLIED_AT)")
    if applied is not None:
        con.execute("INSERT INTO SCHEMA_VERSION VALUES (174, ?)", (v162h._ms(applied),))
    con.execute("CREATE TABLE SOURCE_FRESHNESS_STATE (SOURCE_NAME, LAST_LOAD_TS, STATUS)")
    con.execute("CREATE TABLE APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, LOGGED_AT)")
    con.execute("DROP TABLE ALERT_CONFIG")
    con.execute("CREATE TABLE ALERT_CONFIG (RULE_ID, NAME)")
    if name is not None:
        con.execute("INSERT INTO ALERT_CONFIG VALUES ('SEC_ADMIN_GRANT', ?)", (name,))
    return con


def _checks(con: sqlite3.Connection, grid: str) -> dict[str, str]:
    sql = v162h._sq(grid).replace("LISTAGG(DISTINCT CONTEXT, '; ')", "GROUP_CONCAT(DISTINCT CONTEXT)")
    return dict(con.execute(sql).fetchall())


def _names() -> tuple[str, str]:
    (old, new) = re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_CONFIG\n   SET NAME = '([^']*)'\n"
                            r" WHERE RULE_ID = 'SEC_ADMIN_GRANT'\n   AND NAME = '([^']*)';", _V174)[0][::-1]
    return old, new


def test_part_b_v174_1_reads_ok_on_the_v174_body_only(tmp_path):
    _, pb = _gen(tmp_path)
    grid = _grid(pb, "SELECT 'V174.1 SCHEMA_VERSION", "-- V174.2 ")
    old_name, new_name = _names()
    now = v162h._at(_WED, 10)
    good = _checks(_part_b_con(now, applied=now, body=_H, name=new_name), grid)
    assert len(good) == 6 and set(good.values()) == {"OK"}, good
    stale = _checks(_part_b_con(now, applied=now, body=_H173, name=old_name), grid)
    assert {k for k, v in stale.items() if v != "OK"} == {
        f"V174.1 SP_ALERT_SCAN DDL names {_DSA} 6 times (three admin lists, three notes)",
        "V174.1 SP_ALERT_SCAN DDL has: V174 (owner 2026-10-05)",
        f"V174.1 SEC_ADMIN_GRANT rule name lists {_DSA}"}, stale
    edited = _checks(_part_b_con(now, applied=now, body=_H, name="Admin grants (my wording)"), grid)
    assert edited[f"V174.1 SEC_ADMIN_GRANT rule name lists {_DSA}"].startswith("CHECK: ")
    missing = _checks(_part_b_con(now, applied=None, body=_H, name=None), grid)
    assert missing["V174.1 SCHEMA_VERSION has 174"].startswith("FAIL")
    assert missing[f"V174.1 SEC_ADMIN_GRANT rule name lists {_DSA}"] == "FAIL: no SEC_ADMIN_GRANT rule"


def test_part_b_v174_2_reads_only_a_scan_that_started_after_the_apply(tmp_path):
    _, pb = _gen(tmp_path)
    grid = _grid(pb, "-- V174.2 ", "-- V174.3 ")
    applied = v162h._at(_WED, 10, 20)

    def run(now, beat, status, errors):
        con = _part_b_con(now, applied=applied, body=_H, name=None)
        con.execute("INSERT INTO SOURCE_FRESHNESS_STATE VALUES ('ALERT_SCAN_HOURLY', ?, ?)", (v162h._ms(beat), status))
        for at, context in errors:
            con.execute("INSERT INTO APP_ERROR_LOG VALUES ('AlertScan', 'rule_block_failed', 'x', ?, ?)",
                        (context, v162h._ms(at)))
        return _checks(con, grid)

    ok, bad = "alert scan 14/14 rule blocks ok", "alert scan 13/14 rule blocks ok"
    old_fail = (v162h._at(_WED, 10, 21), "rule SEC_ADMIN_GRANT - other rules unaffected")
    # the 10:15 scan finishes on the OLD body after the apply: WAIT, never FAIL / CHECK
    early = run(v162h._at(_WED, 10, 30), v162h._at(_WED, 10, 22), bad, [old_fail])
    assert sum(v.startswith("WAIT: ") for v in early.values()) == 2 and {v for v in early.values()
                                                                          if not v.startswith("WAIT")} == {"OK"}
    # the next scan (11:15-11:18) on the V174 body: all OK; a failure of one of the three arms is caught
    assert set(run(v162h._at(_WED, 11, 30), v162h._at(_WED, 11, 18), ok, [old_fail]).values()) == {"OK"}
    for rule in ("SEC_NEW_ADMIN_NETWORK", "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT"):
        got = run(v162h._at(_WED, 11, 30), v162h._at(_WED, 11, 18), ok,
                  [(v162h._at(_WED, 11, 16), f"rule {rule} - other rules unaffected")])
        assert got["V174.2 no rule_block_failed of the three V174 arms since the apply"].startswith("FAIL"), rule
    other = run(v162h._at(_WED, 11, 30), v162h._at(_WED, 11, 18), ok,
                [(v162h._at(_WED, 11, 16), "rule PERF_SPILL_GB - other rules unaffected")])
    assert other["V174.2 no rule_block_failed of the three V174 arms since the apply"] == "OK"
    assert other["V174.2 no rule_block_failed of any rule since the apply"].startswith("CHECK")
    assert run(v162h._at(_WED, 11, 30), v162h._at(_WED, 11, 18), bad, [])["V174.2 hourly heartbeat reads 14/14"] \
        == f"CHECK: {bad}"


def test_part_b_v174_3_lists_the_dsa_holders_events_since_the_apply(tmp_path):
    _, pb = _gen(tmp_path)
    grid = _grid(pb, "-- V174.3 ", None)
    applied = v162h._at(_WED, 9)
    grants = [v162h._grant("AL.ICE", _DSA, v162h._at(_WED, 8)),
              v162h._grant("BO.B", _DSA, v162h._at(_WED, 1), deleted=v162h._at(_WED, 2)),     # held, then revoked
              v162h._grant("CY.D", "SYSADMIN", v162h._at(_WED, 8))]
    (g,) = v162h._run(_ARM27, v162h._at(_WED, 10), grants=grants[:1])
    events = [*v162h._raised([g], v162h._at(_WED, 10), "a"),
              v162h._user_event("t1", "SEC_LOGIN_TAKEOVER", "BO.B", raised=v162h._at(_WED, 10)),
              v162h._user_event("t2", "SEC_LOGIN_TAKEOVER", "CY.D", raised=v162h._at(_WED, 10)),  # not a holder
              v162h._user_event("t3", "SEC_LOGIN_TAKEOVER", "AL.ICE", raised=v162h._at(_WED, 8)),  # before the apply
              v162h._user_event("t4", "SEC_CRED_EXPIRY", "AL.ICE", raised=v162h._at(_WED, 10))]   # another rule
    con = v162h._connect({"GRANTS_TO_USERS": grants, "ALERT_EVENTS": events}, v162h._ms(v162h._at(_WED, 11)))
    con.execute("CREATE TABLE SCHEMA_VERSION (VERSION, APPLIED_AT)")
    con.execute("INSERT INTO SCHEMA_VERSION VALUES (174, ?)", (v162h._ms(applied),))
    rows = v162h._rows(con, v162h._sq(grid))
    assert [(r["RULE_ID"], r["TITLE"]) for r in rows] == [
        ("SEC_ADMIN_GRANT", f"Admin role {_DSA} granted to AL.ICE - first time"),
        ("SEC_LOGIN_TAKEOVER", "SEC_LOGIN_TAKEOVER title")]

