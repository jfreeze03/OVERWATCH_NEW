"""Executed harness for V176 -- hourly arm [27] SEC_ADMIN_GRANT skips Snowflake's own Streamlit runtime grant
(owner 2026-10-12: "Whenever I deploy the app, this keeps popping up in Teams. I want to turn this off.").

Every statement runs from the V176 migration's OWN text, next to V174's, on the shared sqlite fixtures of
tests/migrations/test_v162_harness (grants with CREATED_ON / DELETED_ON / GRANTED_BY, the Central clock, the V004 widths
as CHECKs; REGEXP_LIKE is re.fullmatch: anchored and case-sensitive, like Snowflake's default). The live shape (probes
S1/S2, 2026-10-12): one STPLATSTREAMLIT<digits> user per deploy, granted SNOW_ACCOUNTADMINS and SYSTEM$MANAGED by
nobody (GRANTED_BY NULL), both revoked within seconds. What it proves:
  * the change -- that grant raises nothing on V176's arm and one HIGH 'first time ... by ?' row on V174's (teeth);
    the same with SNOW_SYSADMINS (the planned owner);
  * what still raises -- a grantor set, any other admin role, a non-runtime name (the S3 real-user shape), no
    SYSTEM$MANAGED co-grant (or one with a grantor, or one held by another user), look-alike names, any NULL in the
    four conditions (fail open), and a person's re-grant after the system grant ('Re-grant: 1 earlier');
  * nothing else moved -- the V162 / V174 [27] scenarios pass on the V176 arm, V174 / V176 raise the same rows for every
    non-runtime grant, and [26] still makes a runtime holder's takeover CRITICAL;
  * the runbox text -- PREFLIGHT P176.1a / P176.1b / P176.2 and PART B V176.1-V176.3 executed against the same fixtures.
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
from tests.migrations import test_v174_harness as v174h

_DSA = "SNOW_PRI_GFR_PRD_ALFA_DSA"
_RUNTIME = "STPLATSTREAMLIT98004567325"
_MANAGED = "SYSTEM$MANAGED"
_PAIR = ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS")
_V176 = read("snowflake/migrations/V176__alert_scan_streamlit_runtime_grant.sql")
_V174 = read("snowflake/migrations/V174__alert_scan_dsa_admin_role.sql")
_H, _H174 = v162h._proc(_V176, "SP_ALERT_SCAN()"), v162h._proc(_V174, "SP_ALERT_SCAN()")
_S26 = ("    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
_S27 = ("    -- [27] SEC_ADMIN_GRANT", "    IF (MOD(ct_hour, 3) = 2) THEN")
_ARM26, _ARM26_174 = v162h._between(_H, *_S26), v162h._between(_H174, *_S26)
_ARM27, _ARM27_174 = v162h._between(_H, *_S27), v162h._between(_H174, *_S27)
_NOW = v162h._NOW27                                      # Wednesday 2026-09-30 15:00 Central
_PRED = re.search(r"\n        WHERE (NOT COALESCE\(.*, FALSE\))\n", _ARM27).group(1)


def _deploy(user: str = _RUNTIME, role: str = "SNOW_ACCOUNTADMINS", at: datetime | None = None, *,
            co_grant: bool = True, by: str | None = None, co_by: str | None = None) -> list[dict]:
    """One deploy's grants, the live shape: the owner role and SYSTEM$MANAGED, both by nobody, revoked in seconds."""
    at = at or _NOW - timedelta(hours=2)
    gone = at + timedelta(seconds=6)
    rows = [v162h._grant(user, role, at, deleted=gone, by=by)]
    if co_grant:
        rows.append(v162h._grant(user, _MANAGED, at + timedelta(milliseconds=3), deleted=gone, by=co_by))
    return rows


def test_the_arm_texts_are_the_migrations():
    assert _ARM27 != _ARM27_174 and "STPLATSTREAMLIT" not in _ARM27_174 and _ARM26 == _ARM26_174
    assert _ARM27.count(_PRED) == 1 and _PRED.startswith("NOT COALESCE(g.GRANTED_BY IS NULL AND REGEXP_LIKE(")


# ============================================================================================================
# The change: Snowflake's own runtime grant raises nothing (V174: one HIGH first-time row)
# ============================================================================================================
@pytest.mark.parametrize("role", _PAIR)
def test_the_deploy_runtime_grant_raises_nothing(role):
    grants = _deploy(role=role)
    assert v162h._run(_ARM27, _NOW, grants=grants) == []
    # teeth: V174's arm raised it, once per deploy -- the reported Teams line ('by ?', first time, since revoked)
    (old,) = v162h._run(_ARM27_174, _NOW, grants=grants)
    assert old["SEVERITY"] == "HIGH" and old["TITLE"] == f"Admin role {role} granted to {_RUNTIME} - first time"
    assert old["DETAIL"].startswith("Granted 2026-09-30 13:00 Central by ?. First grant of this role to this user on "
                                    "record. Since revoked 2026-09-30 13:00 Central.")
    assert old["DEDUPE_KEY"] == v162h._key27(_RUNTIME, role, _NOW - timedelta(hours=2))


def test_two_hundred_deploys_raise_nothing_and_a_live_grant_too():
    """One user per deploy (S1: 200+ since mid-September), held or already revoked: none raises."""
    grants = [g for i in range(200)
              for g in _deploy(f"STPLATSTREAMLIT{98004566000 + i}", at=_NOW - timedelta(hours=25, minutes=-i))]
    grants += [v162h._grant("STPLATSTREAMLIT7", "SNOW_ACCOUNTADMINS", _NOW - timedelta(minutes=30), by=None),
               v162h._grant("STPLATSTREAMLIT7", _MANAGED, _NOW - timedelta(minutes=30), by=None)]   # still held
    assert v162h._run(_ARM27, _NOW, grants=grants) == []
    assert len(v162h._run(_ARM27_174, _NOW, grants=grants)) == 201


# ============================================================================================================
# What still raises (each case is the skipped shape with ONE condition broken)
# ============================================================================================================
def _one(grants: list[dict]) -> dict:
    (row,) = v162h._run(_ARM27, _NOW, grants=grants)
    assert row["SEVERITY"] == "HIGH" and row["RULE_ID"] == "SEC_ADMIN_GRANT" and row["COMPANY"] == "ALL"
    return row


@pytest.mark.parametrize("by", ["SECURITYADMIN", "JANE.DOE", ""])
def test_a_grant_with_a_grantor_raises_whatever_the_user_is_called(by):
    """(a) broken: any GRANT a person or role runs records its grantor -- also one made to hijack a runtime user."""
    row = _one(_deploy(by=by))
    assert f"Central by {by}. " in row["DETAIL"]
    assert row["TITLE"] == f"Admin role SNOW_ACCOUNTADMINS granted to {_RUNTIME} - first time"


@pytest.mark.parametrize("role", [r for r in (*v162h._ROLES, _DSA) if r not in _PAIR])
def test_any_other_admin_role_to_a_runtime_user_raises(role):
    """(c) broken: the skip covers the app owner pair only (SNOW_ACCOUNTADMINS today, SNOW_SYSADMINS later)."""
    row = _one(_deploy(role=role))
    assert row["TITLE"] == f"Admin role {role} granted to {_RUNTIME} - first time" and "by ?. " in row["DETAIL"]


@pytest.mark.parametrize("user", ["AL.ICE", "SNOWADMIN", "KEBARR1", "WORKBENCH"])
def test_a_no_grantor_grant_to_a_real_user_raises(user):
    """(b) broken -- probe S3: real users have no-grantor admin grants too, so GRANTED_BY IS NULL alone is not the
    marker. Even with a SYSTEM$MANAGED row of their own, a non-runtime name raises."""
    row = _one(_deploy(user=user))
    assert row["TITLE"] == f"Admin role SNOW_ACCOUNTADMINS granted to {user} - first time"


@pytest.mark.parametrize(("label", "grants"), [
    ("no co-grant", _deploy(co_grant=False)),
    ("co-grant made by someone", _deploy(co_by="ACCOUNTADMIN")),
    ("co-grant held by another runtime user",
     [*_deploy(co_grant=False), v162h._grant("STPLATSTREAMLIT1", _MANAGED, _NOW - timedelta(hours=2), by=None)]),
    ("co-grant of another system role",
     [*_deploy(co_grant=False), v162h._grant(_RUNTIME, "SYSTEM$OTHER", _NOW - timedelta(hours=2), by=None)]),
])
def test_a_runtime_shaped_grant_without_the_system_managed_co_grant_raises(label, grants):
    """(d) broken: the system-only co-grant is the marker no customer statement can make; without it, it raises."""
    row = _one(grants)
    assert row["TITLE"].endswith(f"granted to {_RUNTIME} - first time"), label


@pytest.mark.parametrize("user", [
    "stplatstreamlit98004567325",       # a quoted lower-case name: REGEXP_LIKE is case-sensitive
    "STPLATSTREAMLIT",                  # no digits
    "STPLATSTREAMLITX1",                # letters before the digits
    "XSTPLATSTREAMLIT1",                # a prefix (anchored at the start)
    "STPLATSTREAMLIT1X",                # a suffix (anchored at the end)
    "STPLATSTREAMLIT1 ",                # a trailing space
    "STPLATSTREAMLIT1.",
    "STPLAT_STREAMLIT1",
    "STPLATSTREAMLIT١",            # an Arabic-Indic digit, not [0-9]
    "ЅTPLATSTREAMLIT1",            # a Cyrillic look-alike S
])
def test_look_alike_names_raise(user):
    """(b) broken, with every other condition met (the look-alike even holds a no-grantor SYSTEM$MANAGED row)."""
    row = _one(_deploy(user=user))
    assert row["TITLE"].startswith(f"Admin role SNOW_ACCOUNTADMINS granted to {user}")


def test_a_person_regranting_after_the_system_grant_raises_as_a_regrant():
    """PRIOR_GRANTS still reads the full history (the ag CTE is untouched): a later human grant to a runtime user that
    already had the system grant reads 'Re-grant: 1 earlier', names its grantor, and raises."""
    sys_at, human_at = _NOW - timedelta(hours=5), _NOW - timedelta(hours=1)
    grants = [*_deploy(at=sys_at),
              v162h._grant(_RUNTIME, "SNOW_ACCOUNTADMINS", human_at, by="JANE.DOE")]
    row = _one(grants)
    assert row["TITLE"] == f"Admin role SNOW_ACCOUNTADMINS granted to {_RUNTIME}"            # not 'first time'
    assert row["DETAIL"].startswith("Granted 2026-09-30 14:00 Central by JANE.DOE. Re-grant: 1 earlier grant(s)")
    assert row["DEDUPE_KEY"] == v162h._key27(_RUNTIME, "SNOW_ACCOUNTADMINS", human_at)


def test_raised_once_and_a_disabled_rule_raises_nothing():
    grants = _deploy(co_grant=False)
    row = _one(grants)
    assert v162h._run(_ARM27, _NOW + timedelta(hours=1), grants=grants, events=v162h._raised([row], _NOW, "g")) == []
    assert v162h._run(_ARM27, _NOW, grants=grants, cfg=[v162h._cfg("SEC_ADMIN_GRANT", 0, enabled=0)]) == []


# ---- fail open: the predicate on its own, every NULL ---------------------------------------------------------
@pytest.mark.parametrize(("granted_by", "name", "role", "marker", "skipped"), [
    (None, _RUNTIME, "SNOW_ACCOUNTADMINS", _RUNTIME, True),
    (None, _RUNTIME, "SNOW_SYSADMINS", _RUNTIME, True),
    ("X", _RUNTIME, "SNOW_ACCOUNTADMINS", _RUNTIME, False),
    (None, None, "SNOW_ACCOUNTADMINS", _RUNTIME, False),            # a NULL name: REGEXP_LIKE is NULL -> kept
    (None, _RUNTIME, None, _RUNTIME, False),                        # a NULL role -> kept
    (None, _RUNTIME, "SNOW_ACCOUNTADMINS", None, False),            # no rt match -> kept
    (None, _RUNTIME, "ACCOUNTADMIN", _RUNTIME, False),
])
def test_the_predicate_fails_open(granted_by, name, role, marker, skipped):
    con = sqlite3.connect(":memory:")
    con.create_function("REGEXP_LIKE", 2, lambda s, p: None if s is None or p is None
                        else int(re.fullmatch(p, str(s)) is not None))
    (keep,) = con.execute(f"SELECT {_PRED} FROM (SELECT ? AS GRANTED_BY, ? AS GRANTEE_NAME, ? AS ROLE) g, "
                          f"(SELECT ? AS GRANTEE_NAME) rt", (granted_by, name, role, marker)).fetchone()
    assert keep == int(not skipped)


# ============================================================================================================
# Nothing else moved
# ============================================================================================================
_V162_27 = ("test_admin_grant_window_rerun_and_every_role", "test_admin_grant_revoked_regrant_first_time_and_off_hours",
            "test_admin_grant_long_names_fit")


@pytest.mark.parametrize("scenario", _V162_27)
def test_v162_admin_grant_scenarios_hold_on_the_v176_arm(scenario, monkeypatch):
    monkeypatch.setattr(v162h, "_ARM27", _ARM27)
    monkeypatch.setattr(v162h, "_ROLES", (*v162h._ROLES, _DSA))
    getattr(v162h, scenario)()


@pytest.mark.parametrize("scenario", ["test_a_direct_dsa_grant_raises_sec_admin_grant",
                                      "test_a_dsa_grant_off_hours_revoked_or_regranted_says_so_and_fits"])
def test_v174_admin_grant_scenarios_hold_on_the_v176_arm(scenario, monkeypatch):
    monkeypatch.setattr(v174h, "_ARM27", _ARM27)             # (its teeth leg keeps V173's arm)
    getattr(v174h, scenario)()


@pytest.mark.parametrize("role", [*v162h._ROLES, _DSA, "ANALYST", _MANAGED])
@pytest.mark.parametrize("by", ["SEC_OPS_BOB", None])
def test_v174_and_v176_raise_the_same_rows_for_every_non_runtime_grant(role, by):
    grants = [v162h._grant("AL.ICE", role, _NOW - timedelta(hours=2), by=by),
              v162h._grant("BO.B", role, _NOW - timedelta(hours=30), by=by),
              v162h._grant("CY.D", role, _NOW - timedelta(days=40), deleted=_NOW - timedelta(days=39), by=by),
              v162h._grant("CY.D", role, _NOW - timedelta(hours=3), by=by)]
    assert v162h._run(_ARM27, _NOW, grants=grants) == v162h._run(_ARM27_174, _NOW, grants=grants)


def test_a_runtime_holders_takeover_is_still_critical():
    """[26] is V174's byte for byte: a runtime user that held the owner role at a burst-then-success is CRITICAL (the
    users never log in, probe S4, so it cannot fire today; if it ever does it is real news)."""
    s = v162h._at(v162h._WED, 10)
    grants = [v162h._grant(_RUNTIME, "SNOW_ACCOUNTADMINS", s - timedelta(minutes=50), by=None),
              v162h._grant(_RUNTIME, _MANAGED, s - timedelta(minutes=50), by=None)]
    (row,) = v162h._run(_ARM26, s + timedelta(hours=1), logins=v162h._episode(_RUNTIME, s), grants=grants)
    assert row["SEVERITY"] == "CRITICAL" and row["TITLE"].endswith(" (admin role SNOW_ACCOUNTADMINS)")


# ============================================================================================================
# PREFLIGHT P176.1a / P176.1b / P176.2 and PART B V176.1-V176.3, executed
# ============================================================================================================
def _gen(tmp_path) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("V176_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(V176_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(tmp_path / "pf.sql"),
               PART_B_OUT=str(tmp_path / "pb.sql"))
    out = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v176.py")], env=env, cwd=tmp_path,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _V176
    return (tmp_path / "pf.sql").read_text(encoding="utf-8"), (tmp_path / "pb.sql").read_text(encoding="utf-8")


def _grid(text: str, head: str, nxt: str | None) -> str:
    g = text[text.index(head):text.index(nxt) if nxt else len(text)]
    return "\n".join(ln for ln in g.splitlines() if not ln.startswith("--")).strip().rstrip(";")


_LISTAGG = re.compile(r"LISTAGG\(DISTINCT (.+?), '(?:; |, )'\)")


def _sql(grid: str) -> str:
    return v162h._sq(_LISTAGG.sub(r"GROUP_CONCAT(DISTINCT \1)", grid))


def _fixture() -> list[dict]:
    """The live mix: two deploys (one inside 26h), a person's grant inside 26h, a real user's no-grantor grant (S3)
    10 days ago, a runtime-shaped grant with no co-grant 5 days ago, and a grant older than 30 days."""
    return [*_deploy(at=_NOW - timedelta(hours=3)),
            *_deploy("STPLATSTREAMLIT98004566893", at=_NOW - timedelta(days=4)),
            v162h._grant("AL.ICE", "SYSADMIN", _NOW - timedelta(hours=5), by="SEC_OPS_BOB"),
            v162h._grant("KEBARR1", "ACCOUNTADMIN", _NOW - timedelta(days=10), by=None),
            *_deploy("STPLATSTREAMLIT55", at=_NOW - timedelta(days=5), co_grant=False),
            v162h._grant("OLD.USER", "SYSADMIN", _NOW - timedelta(days=31))]


def test_preflight_p176_1a_counts_what_each_window_raises_today_and_after(tmp_path):
    pf, _ = _gen(tmp_path)
    grants = _fixture()
    con = v162h._connect({"GRANTS_TO_USERS": grants}, v162h._ms(_NOW))
    rows = {r["WINDOW_NAME"]: r for r in v162h._rows(con, _sql(_grid(pf, "-- P176.1a ", "-- P176.1b ")))}
    assert list(rows) == ["last 26h", "last 30 days"]
    day, month = rows["last 26h"], rows["last 30 days"]
    assert (day["V174_RAISES"], day["V176_SKIPS"], day["V176_STILL_RAISES"], day["RUNTIME_USERS_SKIPPED"]) == (2, 1, 1, 1)
    assert (month["V174_RAISES"], month["V176_SKIPS"], month["V176_STILL_RAISES"],
            month["RUNTIME_USERS_SKIPPED"]) == (5, 2, 3, 2)
    assert day["STILL_RAISED"] == "AL.ICE (SYSADMIN)"
    assert set(month["STILL_RAISED"].split(",")) == {"AL.ICE (SYSADMIN)", "KEBARR1 (ACCOUNTADMIN)",
                                                     "STPLATSTREAMLIT55 (SNOW_ACCOUNTADMINS)"}
    # the 26h window is exactly what one scan reads: V174's arm and V176's arm raise those counts
    assert len(v162h._run(_ARM27_174, _NOW, grants=grants)) == day["V174_RAISES"]
    assert len(v162h._run(_ARM27, _NOW, grants=grants)) == day["V176_STILL_RAISES"]


def test_preflight_p176_1b_lists_every_grant_with_its_verdict(tmp_path):
    pf, _ = _gen(tmp_path)
    con = v162h._connect({"GRANTS_TO_USERS": _fixture()}, v162h._ms(_NOW))
    rows = v162h._rows(con, _sql(_grid(pf, "-- P176.1b ", "-- P176.2 ")))
    got = {(r["GRANTEE_NAME"], r["ROLE"]): (r["V176_VERDICT"], r["IN_LAST_26H"], r["HAS_SYSTEM_MANAGED_COGRANT"])
           for r in rows}
    assert got == {(_RUNTIME, "SNOW_ACCOUNTADMINS"): ("skipped by V176", 1, 1),
                   ("STPLATSTREAMLIT98004566893", "SNOW_ACCOUNTADMINS"): ("skipped by V176", 0, 1),
                   ("AL.ICE", "SYSADMIN"): ("still raises", 1, 0),
                   ("KEBARR1", "ACCOUNTADMIN"): ("still raises", 0, 0),
                   ("STPLATSTREAMLIT55", "SNOW_ACCOUNTADMINS"): ("still raises", 0, 0)}
    assert [r["V176_VERDICT"] for r in rows] == sorted(r["V176_VERDICT"] for r in rows)
    assert all(r["GRANTED_BY"] is None for r in rows if r["V176_VERDICT"] == "skipped by V176")
    # the skipped rows are exactly the ones the arm itself drops (same text, same verdict)
    in_day = {(r["GRANTEE_NAME"], r["ROLE"]) for r in rows if r["IN_LAST_26H"] and r["V176_VERDICT"] == "still raises"}
    assert in_day == {tuple(r["DEDUPE_KEY"].split("|")[1:3]) for r in v162h._run(_ARM27, _NOW, grants=_fixture())}


def test_preflight_p176_2_lists_only_the_open_runtime_system_grant_events(tmp_path):
    pf, _ = _gen(tmp_path)
    grid = _grid(pf, "-- P176.2 ", None)
    raised = _NOW - timedelta(hours=1)
    other = "STPLATSTREAMLIT11"
    grants = [*_deploy(at=_NOW - timedelta(hours=3)),
              *_deploy(other, at=_NOW - timedelta(hours=4), co_grant=False),
              v162h._grant("STPLATSTREAMLIT22", "SNOW_SYSADMINS", _NOW - timedelta(hours=5), by=None),
              v162h._grant("STPLATSTREAMLIT22", _MANAGED, _NOW - timedelta(hours=5), by=None),
              v162h._grant("STPLATSTREAMLIT33", "SNOW_ACCOUNTADMINS", _NOW - timedelta(hours=6), by=None),
              v162h._grant("STPLATSTREAMLIT33", _MANAGED, _NOW - timedelta(hours=6), by=None),
              v162h._grant("STPLATSTREAMLIT44", "SNOW_ACCOUNTADMINS", _NOW - timedelta(hours=7), by="JANE.DOE"),
              v162h._grant("STPLATSTREAMLIT44", "SYSADMIN", _NOW - timedelta(hours=8), by=None),
              v162h._grant("AL.ICE", "SNOW_ACCOUNTADMINS", _NOW - timedelta(hours=9), by=None)]
    rows = {r["DEDUPE_KEY"].split("|")[1] + "|" + r["DEDUPE_KEY"].split("|")[2]: r
            for r in v162h._run(_ARM27_174, _NOW, grants=grants)}
    assert len(rows) == 7
    status = {f"{_RUNTIME}|SNOW_ACCOUNTADMINS": "OPEN", f"{other}|SNOW_ACCOUNTADMINS": "ACK",
              "STPLATSTREAMLIT22|SNOW_SYSADMINS": "SNOOZED", "STPLATSTREAMLIT33|SNOW_ACCOUNTADMINS": "RESOLVED",
              "STPLATSTREAMLIT44|SNOW_ACCOUNTADMINS": "OPEN", "STPLATSTREAMLIT44|SYSADMIN": "OPEN",
              "AL.ICE|SNOW_ACCOUNTADMINS": "OPEN"}
    events = [dict(e, STATUS=status[k]) for k, r in rows.items() for e in v162h._raised([r], raised, k)]
    con = v162h._connect({"GRANTS_TO_USERS": grants, "ALERT_EVENTS": events}, v162h._ms(_NOW))
    got = {r["TITLE"].split(" granted to ")[1].split(" ")[0]: (r["STATUS"], r["HAS_SYSTEM_MANAGED_COGRANT"])
           for r in v162h._rows(con, _sql(grid))}
    # the open / ack / snoozed system grants of the owner pair to runtime names; a missing co-grant is flagged
    assert got == {_RUNTIME: ("OPEN", 1), other: ("ACK", 0), "STPLATSTREAMLIT22": ("SNOOZED", 1)}


def _part_b_con(now: datetime, *, applied: datetime | None, body: str, enabled: int | None = 1,
                grants=(), events=()) -> sqlite3.Connection:
    cfg = [] if enabled is None else [v162h._cfg("SEC_ADMIN_GRANT", 0, enabled=enabled)]
    con = v162h._connect({"ALERT_CONFIG": cfg, "GRANTS_TO_USERS": list(grants), "ALERT_EVENTS": list(events)},
                         v162h._ms(now))
    # (the translation drops the DBA_MAINT_DB.OVERWATCH. prefix, inside the GET_DDL literal too)
    con.create_function("GET_DDL", 2, lambda kind, obj: body if (kind, obj) == ("PROCEDURE", "SP_ALERT_SCAN()")
                        else None)
    con.create_function("CONTAINS", 2, lambda a, b: None if a is None or b is None else int(b in a))
    con.create_function("REGEXP_COUNT", 2, lambda a, p: None if a is None else len(re.findall(p, a)))
    con.execute("CREATE TABLE SCHEMA_VERSION (VERSION, APPLIED_AT)")
    if applied is not None:
        con.execute("INSERT INTO SCHEMA_VERSION VALUES (176, ?)", (v162h._ms(applied),))
    con.execute("CREATE TABLE SOURCE_FRESHNESS_STATE (SOURCE_NAME, LAST_LOAD_TS, STATUS)")
    con.execute("CREATE TABLE APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, LOGGED_AT)")
    return con


def _checks(con: sqlite3.Connection, grid: str) -> dict[str, str]:
    return dict(con.execute(_sql(grid)).fetchall())


def test_part_b_v176_1_reads_ok_on_the_v176_body_only(tmp_path):
    _, pb = _gen(tmp_path)
    grid = _grid(pb, "SELECT 'V176.1 SCHEMA_VERSION", "-- V176.2 ")
    now = v162h._at(v162h._WED, 10)
    good = _checks(_part_b_con(now, applied=now, body=_H), grid)
    assert len(good) == 10 and set(good.values()) == {"OK"}, good
    stale = _checks(_part_b_con(now, applied=now, body=_H174), grid)
    assert {k for k, v in stale.items() if v != "OK"} == {
        "V176.1 SP_ALERT_SCAN DDL names STPLATSTREAMLIT 3 times (rt CTE, skip predicate, note)",
        "V176.1 SP_ALERT_SCAN DDL has: STPLATSTREAMLIT[0-9]+",
        "V176.1 SP_ALERT_SCAN DDL has: g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME",
        "V176.1 SP_ALERT_SCAN DDL has: rt.GRANTEE_NAME IS NOT NULL",
        f"V176.1 SP_ALERT_SCAN DDL has: {_MANAGED}", "V176.1 SP_ALERT_SCAN DDL has: V176 (owner 2026-10-12)"}, stale
    off = _checks(_part_b_con(now, applied=now, body=_H, enabled=0), grid)
    assert off["V176.1 SEC_ADMIN_GRANT is enabled"].startswith("FAIL: the rule is disabled")
    missing = _checks(_part_b_con(now, applied=None, body=_H, enabled=None), grid)
    assert missing["V176.1 SCHEMA_VERSION has 176"].startswith("FAIL")
    assert missing["V176.1 SEC_ADMIN_GRANT is enabled"] == "FAIL: no SEC_ADMIN_GRANT rule"


def test_part_b_v176_2_reads_only_a_scan_that_started_after_the_apply(tmp_path):
    _, pb = _gen(tmp_path)
    grid = _grid(pb, "-- V176.2 ", "-- V176.3 ")
    applied = v162h._at(v162h._WED, 10, 20)

    def run(now, beat, status, errors):
        con = _part_b_con(now, applied=applied, body=_H)
        con.execute("INSERT INTO SOURCE_FRESHNESS_STATE VALUES ('ALERT_SCAN_HOURLY', ?, ?)", (v162h._ms(beat), status))
        for at, context in errors:
            con.execute("INSERT INTO APP_ERROR_LOG VALUES ('AlertScan', 'rule_block_failed', 'boom', ?, ?)",
                        (context, v162h._ms(at)))
        return _checks(con, grid)

    ok, bad = "alert scan 14/14 rule blocks ok", "alert scan 13/14 rule blocks ok"
    old_fail = (v162h._at(v162h._WED, 10, 21), "rule SEC_ADMIN_GRANT - other rules unaffected")
    early = run(v162h._at(v162h._WED, 10, 30), v162h._at(v162h._WED, 10, 22), bad, [old_fail])
    assert sum(v.startswith("WAIT: ") for v in early.values()) == 2
    assert {v for v in early.values() if not v.startswith("WAIT")} == {"OK"}
    later = (v162h._at(v162h._WED, 11, 30), v162h._at(v162h._WED, 11, 18))
    assert set(run(*later, ok, [old_fail]).values()) == {"OK"}
    mine = run(*later, ok, [(v162h._at(v162h._WED, 11, 16), "rule SEC_ADMIN_GRANT - other rules unaffected")])
    assert mine["V176.2 no rule_block_failed of SEC_ADMIN_GRANT since the apply"] == "FAIL: boom"
    other = run(*later, ok, [(v162h._at(v162h._WED, 11, 16), "rule PERF_SPILL_GB - other rules unaffected")])
    assert other["V176.2 no rule_block_failed of SEC_ADMIN_GRANT since the apply"] == "OK"
    assert other["V176.2 no rule_block_failed of any rule since the apply"].startswith("CHECK")
    assert run(*later, bad, [])["V176.2 hourly heartbeat reads 14/14"] == f"CHECK: {bad}"


_C1, _C2, _C3, _C4 = ("V176.3 a deploy since the apply is on record",
                      f"V176.3 each runtime user since the apply has the {_MANAGED} co-grant",
                      "V176.3 no SEC_ADMIN_GRANT event for a no-grantor runtime grant since the apply",
                      "V176.3 every other admin grant since the apply raised")


def _v176_3(tmp_path, grants: list[dict], *, scan: str = _ARM27) -> dict[str, str]:
    """Apply at 09:00, the hourly scans through 15:00 run ``scan`` (V176's arm, or V174's for a scan that never got the
    new body), then PART B V176.3 reads at 15:30."""
    _, pb = _gen(tmp_path)
    applied, now = v162h._at(v162h._WED, 9), v162h._at(v162h._WED, 15, 30)
    events: list[dict] = []
    for h in range(10, 16):
        at = v162h._at(v162h._WED, h, 7)
        new = v162h._run(scan, at, grants=[g for g in grants if g["CREATED_ON"] <= v162h._ms(at)], events=events)
        events += v162h._raised(new, at, f"h{h}-")
    con = _part_b_con(now, applied=applied, body=_H, grants=grants, events=events)
    return _checks(con, _grid(pb, "-- V176.3 ", None))


def test_part_b_v176_3_waits_for_a_deploy(tmp_path):
    got = _v176_3(tmp_path, [v162h._grant("AL.ICE", "SYSADMIN", v162h._at(v162h._WED, 10), by="SEC_OPS_BOB")])
    assert [got[c].split(":")[0] for c in (_C1, _C2, _C3)] == ["WAIT"] * 3 and got[_C4] == "OK"


def test_part_b_v176_3_reads_ok_after_a_clean_deploy(tmp_path):
    grants = [*_deploy(at=v162h._at(v162h._WED, 11)),
              *_deploy("STPLATSTREAMLIT8", at=v162h._at(v162h._WED, 8)),                 # before the apply
              v162h._grant("AL.ICE", "SYSADMIN", v162h._at(v162h._WED, 10), by="SEC_OPS_BOB")]
    got = _v176_3(tmp_path, grants)
    assert got == {_C1: "OK (1 runtime user(s))", _C2: "OK", _C3: "OK", _C4: "OK"}


def test_part_b_v176_3_catches_a_missing_co_grant_and_an_old_body(tmp_path):
    no_co = _v176_3(tmp_path, _deploy(at=v162h._at(v162h._WED, 11), co_grant=False))
    assert no_co[_C2].startswith(f"CHECK: {_RUNTIME} lack it") and no_co[_C3].startswith("FAIL: Admin role")
    assert no_co[_C4] == "OK"                    # it raised, as the fail-open rule says
    old = _v176_3(tmp_path, _deploy(at=v162h._at(v162h._WED, 11)), scan=_ARM27_174)
    assert old[_C2] == "OK" and old[_C3] == (f"FAIL: Admin role SNOW_ACCOUNTADMINS granted to {_RUNTIME} - first time")


def test_part_b_v176_3_positive_control_catches_a_real_miss(tmp_path):
    """A person's admin grant made after the apply and 3h or older must have raised; one that did not is a FAIL
    (here: a scan that skipped it, simulated by never scanning); a younger one is not judged yet."""
    _, pb = _gen(tmp_path)
    applied, now = v162h._at(v162h._WED, 9), v162h._at(v162h._WED, 15, 30)
    grants = [*_deploy(at=v162h._at(v162h._WED, 11)),
              v162h._grant("AL.ICE", "SYSADMIN", v162h._at(v162h._WED, 10), by="SEC_OPS_BOB"),
              v162h._grant(_RUNTIME, "SNOW_ACCOUNTADMINS", v162h._at(v162h._WED, 11, 30), by="JANE.DOE"),
              v162h._grant("YOUNG.ONE", "SYSADMIN", v162h._at(v162h._WED, 14), by="SEC_OPS_BOB")]
    got = _checks(_part_b_con(now, applied=applied, body=_H, grants=grants), _grid(pb, "-- V176.3 ", None))
    assert got[_C4].startswith("FAIL: no event for ")
    assert set(got[_C4][len("FAIL: no event for "):].split(",")) == {"AL.ICE (SYSADMIN)",
                                                                   f"{_RUNTIME} (SNOW_ACCOUNTADMINS)"}
    assert _v176_3(tmp_path, grants)[_C4] == "OK"                    # the V176 arm raised both
