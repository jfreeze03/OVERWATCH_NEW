"""Admin ▸ App access (v4.610.0, owner decision 2026-10-05): the pure readers behind the tab.

Two different questions, answered in two different places:
  * who can OPEN the app is Snowflake's USAGE on the Streamlit. The decision names config.APP_ACCESS_ROLES as
    the roles to hold it, and app_grant_review compares a SHOW GRANTS ON STREAMLIT answer with that set.
    Since 4.610.1 snowflake/roles.sql grants all four USAGE on the database, schema and app, and its
    -20011/-20012 proof block is this module's rule (tests/test_admin_access_tab.py locks both, and pins
    config.ROLES_SQL_APP_GRANTEES to the roles roles.sql grants). Every remedy sentence is still split on that
    tuple, so a role the decision names but roles.sql does not grant (none today) would never be told to
    re-run roles.sql, which cannot add it;
  * who can CHANGE things is decided in-app per viewer (app.core.session.viewer_access): the named
    OPERATOR_USERS, or a DIRECT user grant of config.ADMIN_ACCESS_ROLE looked up live as the owner. Everyone
    else who can open the app gets the view-only VIEWER_UNKNOWN_PROFILE.

These helpers turn the session's access snapshot (session.access_info) and the SHOW answer into plain
rows and sentences. Pure: no Streamlit, no SQL, no server clock (``now`` is passed in).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from zoneinfo import ZoneInfo

from app.config import (
    ACCESS_TTL_S,
    ADMIN_ACCESS_ROLE,
    APP_ACCESS_ROLES,
    PAGES_BY_PROFILE,
    ROLES_SQL_APP_GRANTEES,
    VIEW_ACCESS_ROLE,
    VIEWER_UNKNOWN_PROFILE,
    access_retry_s,
)
from app.logic.formulas import ACCOUNT_TIMEZONE, humanize_duration

#: The roles meant to hold USAGE on the app, and the only ones that may (the 2026-10-05 decision's target set).
EXPECTED_APP_GRANTEES: tuple[str, ...] = tuple(APP_ACCESS_ROLES)
#: The expected roles roles.sql grants USAGE, and the only ones its proof block accepts (all four since 4.610.1).
ROLES_SQL_MANAGED: tuple[str, ...] = tuple(ROLES_SQL_APP_GRANTEES)

#: STATUS for an expected role with no USAGE grant, by whether roles.sql grants it (one wording, many readers).
#: MISSING_PENDING and HELD_AHEAD apply only to an access role roles.sql does not grant: none since 4.610.1.
MISSING_RERUN = ("Missing: re-run snowflake/roles.sql's Streamlit block (every deploy re-creates the app and drops "
                 "its USAGE grants; the proof block raises -20012 meanwhile)")
MISSING_PENDING = ("Missing: not granted by roles.sql yet (owner-side change pending); roles.sql's current "
                   "proof block raises -20011 on its next run once this role holds USAGE")
#: STATUS for an expected role that holds USAGE although roles.sql does not grant it (a hand-made grant).
HELD_AHEAD = ("OK, but not granted by roles.sql yet (owner-side change pending): its current proof block "
              "raises -20011 on its next run while this role holds USAGE")


def pending_roles(roles: Iterable[str] = EXPECTED_APP_GRANTEES) -> tuple[str, ...]:
    """The access roles roles.sql does not grant yet (in order). Empty since 4.610.1, when roles.sql began
    granting all four; kept so a role the decision adds ahead of roles.sql is never told to re-run it."""
    managed = {str(r).upper() for r in ROLES_SQL_MANAGED}
    return tuple(str(r).upper() for r in roles if str(r).upper() not in managed)


def admin_reach_note(holds_usage: bool | None) -> str:
    """The 'Make someone an admin' caveat on Admin ▸ App access, gated on the live grant review (final review #1).

    ``holds_usage`` is whether SHOW GRANTS ON STREAMLIT lists ADMIN_ACCESS_ROLE with USAGE: True / False, or None
    when that read failed or came back empty. Empty while roles.sql grants the role, as it does since 4.610.1
    (the grant review above the guidance covers a lost grant). Only if roles.sql did not grant it would the
    caveat apply: membership makes an admin only of someone who can open the app, so a hand-made grant (True)
    lets members in through the role itself; otherwise (False, or unknown) the 'until' wording holds whichever
    way the grants stand."""
    if ADMIN_ACCESS_ROLE not in pending_roles():
        return ""
    lead = " Membership makes an admin only of someone who can open the app"
    if holds_usage:
        return (f"{lead}: {ADMIN_ACCESS_ROLE} holds USAGE on the app (above), a grant roles.sql does not make "
                "yet, so its members open the app through it, provided it also holds USAGE on the database and "
                "schema.")
    return (f"{lead}: until {ADMIN_ACCESS_ROLE} holds USAGE on the database, schema and app (roles.sql does not "
            "grant it yet), that means a member who also holds another role with USAGE on it.")

_VIEW_PAGES = " and ".join(PAGES_BY_PROFILE.get(VIEWER_UNKNOWN_PROFILE, ()))

#: How each viewer-access source reads on the tab (session.ACCESS_SOURCES plus the two unidentified paths).
SOURCE_LABELS: dict[str, str] = {
    "allowlist": "Named admin (config OPERATOR_USERS): no lookup needed",
    "role": f"Direct member of {ADMIN_ACCESS_ROLE} (live lookup)",
    "default": "Not an admin: the view-only default",
    "lookup_failed": "Admin-role lookup failed: read-only until it recovers",
    "unverified": "Admin-role lookup listed no user (unverified): read-only until it recovers",
    "no_identity": "No viewer identity: view-only, never an admin",
    "off_sis": "Off Streamlit-in-Snowflake (local dev): the role -> profile map decides",
}

#: What holding USAGE on the app means for each expected role (the grants table's MEANS column). Which
#: role OWNS the app is read from the SHOW answer (app_grant_review's OWNER_MEANING), never assumed here.
ROLE_MEANING: dict[str, str] = {
    "SNOW_ACCOUNTADMINS": ("Opens the app. A holder is an admin only via OPERATOR_USERS or a direct admin-role "
                           "grant; otherwise view-only."),
    "SNOW_SYSADMINS": ("Opens the app. A holder is an admin only via OPERATOR_USERS or a direct admin-role "
                       "grant; otherwise view-only."),
    ADMIN_ACCESS_ROLE: ("Opens the app. A DIRECT user member is an OVERWATCH admin: every page and every "
                        "in-app change. A role granted this role is not expanded."),
    VIEW_ACCESS_ROLE: f"Opens the app. View-only: {_VIEW_PAGES}, no changes.",
}

#: Prefixed to MEANS for a role the SHOW answer lists with OWNERSHIP (ownership implies every privilege).
OWNER_MEANING = "Owns the app (OWNERSHIP): every viewer's SQL runs with this role's rights. "


def recheck_note(source: object, *, sis: bool, unavailable: Iterable[str]) -> str:
    """When this viewer's access answer is resolved again (Admin ▸ App access's 'Resolved' help).

    Mirrors session.viewer_access: nothing is memoized for an unidentified viewer or anywhere off
    Streamlit-in-Snowflake; a source in ``unavailable`` (session.ACCESS_UNAVAILABLE_SOURCES: the lookup
    failed or listed no user) is retried on config.access_retry_s's backoff; any other answer after ACCESS_TTL_S."""
    key = str(source or "")
    if not sis or key in ("no_identity", "off_sis"):
        return "Resolved again on every run: no lookup answer is kept."
    if key in tuple(unavailable):
        return (f"The admin-role lookup could not answer, so it is retried {retry_schedule()} "
                "while it keeps failing.")
    return f"A resolved answer is re-checked after {humanize_duration(ACCESS_TTL_S)}."


def retry_schedule() -> str:
    """The failed-lookup retry backoff in words (config.access_retry_s), e.g. 'after 1m, then 2m, 4m, then every
    5m'."""
    steps: list[int] = []
    n = 1
    while True:
        s = access_retry_s(n)
        if steps and s == steps[-1]:
            break
        steps.append(s)
        n += 1
    words = [humanize_duration(s) for s in steps]
    if len(words) == 1:
        return f"every {words[0]}"
    if len(words) == 2:
        return f"after {words[0]}, then every {words[1]}"
    return f"after {words[0]}, then {', '.join(words[1:-1])}, then every {words[-1]}"


def source_label(source: object) -> str:
    """The plain-language reading of a viewer-access source; an unknown source reads as itself."""
    key = str(source or "")
    return SOURCE_LABELS.get(key, key)


def profile_pages(profile: object) -> tuple[str, ...]:
    """The pages a navigation profile offers (empty for an unknown profile)."""
    return tuple(PAGES_BY_PROFILE.get(str(profile or ""), ()))


def resolved_clock(at: object) -> str:
    """An epoch-seconds stamp as 'HH:MM:SS Central' (the account timezone), or the em-dash when unreadable."""
    try:
        stamp = float(at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if stamp != stamp or stamp <= 0:
        return "—"
    try:
        local = dt.datetime.fromtimestamp(stamp, tz=ZoneInfo(ACCOUNT_TIMEZONE))
    except (OverflowError, OSError, ValueError):
        return "—"
    return f"{local:%H:%M:%S} Central"


def _age(at: object, now: float) -> str:
    try:
        return humanize_duration(max(0.0, float(now) - float(at)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def roster_summary(info: Mapping, *, now: float) -> dict[str, str]:
    """The admin-role lookup as one verdict: {state, headline, detail}.

    state: 'ok' (the lookup answered with at least one direct USER grantee), 'unavailable' (it failed, or it
    answered with no USER grantee -- SHOW lists only what the owner role can see, so an empty answer is a
    privilege gap or an empty role, read-only until it recovers, and never a clean "no members"), or
    'not_checked' (no lookup ran in this session: off Streamlit-in-Snowflake)."""
    role = str(info.get("admin_role") or ADMIN_ACCESS_ROLE)
    lookup = str(info.get("roster_lookup") or f"SHOW GRANTS OF ROLE {role}")   # V175: the CALL once applied
    status = str(info.get("roster_status") or "not_checked")
    detail = str(info.get("roster_error") or "")
    if status == "ok":
        n = len(tuple(info.get("admin_users") or ()))
        noun = "member" if n == 1 else "members"
        return {"state": "ok", "detail": "",
                "headline": (f"Lookup OK: {n} direct user {noun} of {role} "
                             f"(checked {_age(info.get('roster_at'), now)} ago).")}
    if status == "lookup_failed":
        return {"state": "unavailable", "detail": detail,
                "headline": (f"The admin-role lookup failed ({lookup}). Viewers not on "
                             "OPERATOR_USERS are read-only until it recovers.")}
    if status == "unverified":
        return {"state": "unavailable", "detail": detail,
                "headline": (f"The admin-role lookup is unverified: {lookup} listed no USER grantee of "
                             f"{role}. That is a privilege gap or an empty role, never read as 'no members'. "
                             "Viewers not on OPERATOR_USERS are read-only until it lists them.")}
    return {"state": "not_checked", "detail": "",
            "headline": ("The admin-role lookup runs only on Streamlit-in-Snowflake, as the app owner. Off "
                         "SiS the role -> profile map decides access.")}


def lookup_check_hint(info: Mapping) -> str:
    """What to run by hand when the admin-role lookup is unavailable, for the statement it actually ran.

    SHOW (before V175) answers as the app owner, so check it as that role. V175's CALL answers as the
    procedure's owner: an error there means the app owner cannot CALL it (no USAGE, or the procedure is gone),
    and no USER row means the procedure's owner cannot see the role's grants."""
    role = str(info.get("admin_role") or ADMIN_ACCESS_ROLE)
    lookup = str(info.get("roster_lookup") or "")
    if lookup.upper().startswith("CALL "):
        return (f"Check: run {lookup} as the role that owns the app (USE SECONDARY ROLES NONE). An error means "
                "that role cannot CALL it: re-run V175, which re-creates the procedure and grants USAGE to "
                "SNOW_SYSADMINS. If it lists no granted_to = USER row, the procedure's owner (the role that "
                f"applied V175) cannot see {role}'s grants. The named admins are unaffected.")
    return (f"Check: run SHOW GRANTS OF ROLE {role} as SNOW_ACCOUNTADMINS (USE SECONDARY ROLES NONE). It must "
            "list each member as a granted_to = USER row; if it errors or lists none, the owner role cannot see "
            "the role's grants. The named admins are unaffected.")


def _row_mapping(row: object) -> dict:
    if isinstance(row, Mapping):
        raw = dict(row)
    elif hasattr(row, "asDict"):
        raw = row.asDict()
    else:
        raw = dict(row)  # type: ignore[call-overload]
    return {str(k).strip().strip('"').lower(): v for k, v in raw.items()}


def _records(rows: object) -> list[dict]:
    if rows is None:
        return []
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    if not isinstance(rows, Iterable):
        return []
    return [_row_mapping(r) for r in rows]


def _cell(m: Mapping, key: str) -> str:
    return str(m.get(key) or "").strip().upper()


def app_grant_review(rows: object, expected: tuple[str, ...] = EXPECTED_APP_GRANTEES) -> dict:
    """Compare a SHOW GRANTS ON STREAMLIT answer with the roles meant (and alone allowed) to open the app.

    The rule is the 2026-10-05 decision's four-role set, the same rule roles.sql's proof block applies since
    4.610.1: a USAGE row is allowed only when granted_to = 'ROLE' and the grantee is one
    of ``expected`` (so a database role, application role or share spelled like an access role is still
    unexpected), and every expected role must hold USAGE. Other privileges (OWNERSHIP) never count either way;
    their grantees are reported as ``owners``, and an owning role's MEANS says so (ownership implies every
    privilege, so it still opens the app while its explicit USAGE grant is missing).

    The remedy is split on ROLES_SQL_MANAGED (what roles.sql grants: all four since 4.610.1): a missing role
    roles.sql grants reads MISSING_RERUN; only an access role roles.sql does not grant (none today) would read
    MISSING_PENDING when missing (re-running roles.sql adds nothing for it) or HELD_AHEAD when it holds USAGE
    anyway (the proof block raises -20011 on it). Every unexpected grantee, whatever its kind, is promised
    -20011: the proof block counts any USAGE row that is not a ROLE named in the four.

    Returns {status: ok | drift | empty, rows, present, missing, missing_rerun, missing_pending, ahead,
    unexpected, owners, table}. An empty answer is 'empty' (the owner always sees its own OWNERSHIP row, so
    nothing at all means the read could not see the app): unverified, never clean. ``table`` lists every
    expected role and every unexpected grantee with EXPECTED / HAS_USAGE / STATUS and what the role MEANS."""
    expected = tuple(str(r).upper() for r in expected)
    records = _records(rows)
    usage: list[tuple[str, str]] = []
    owners: set[str] = set()
    for m in records:
        name, kind, priv = _cell(m, "grantee_name"), _cell(m, "granted_to"), _cell(m, "privilege")
        if not name:
            continue
        if priv == "USAGE":
            usage.append((kind, name))
        elif priv == "OWNERSHIP":
            owners.add(name)
    have = {name for kind, name in usage if kind == "ROLE" and name in expected}
    present = tuple(r for r in expected if r in have)
    missing = tuple(r for r in expected if r not in have)
    pending = set(pending_roles(expected))
    missing_rerun = tuple(r for r in missing if r not in pending)
    missing_pending = tuple(r for r in missing if r in pending)
    ahead = tuple(r for r in present if r in pending)
    unexpected = tuple(sorted({f"{kind or '?'} {name}" for kind, name in usage
                               if not (kind == "ROLE" and name in expected)}))
    if not records:
        status = "empty"
    elif missing or unexpected:
        status = "drift"
    else:
        status = "ok"
    table: list[dict[str, str]] = []
    for role in expected:
        ok = role in have
        owns = role in owners
        if ok:
            status_text = HELD_AHEAD if role in pending else "OK"
        else:
            status_text = MISSING_PENDING if role in pending else MISSING_RERUN
            if owns:
                status_text = ("Missing: no explicit USAGE grant (it owns the app, so it still opens it); "
                               + status_text.removeprefix("Missing: "))
        table.append({"GRANTEE": role, "KIND": "ROLE", "EXPECTED": "Yes", "HAS_USAGE": "Yes" if ok else "No",
                      "STATUS": status_text,
                      "MEANS": (OWNER_MEANING if owns else "") + ROLE_MEANING.get(role, "")})
    for kind, name in sorted({(k, n) for k, n in usage if not (k == "ROLE" and n in expected)}):
        # roles.sql's proof block (4.610.1) counts every USAGE row that is not an access ROLE, whatever its kind
        table.append({"GRANTEE": name, "KIND": kind or "?", "EXPECTED": "No", "HAS_USAGE": "Yes",
                      "STATUS": ("Unexpected: REVOKE it (roles.sql's proof block raises -20011)" if kind == "ROLE"
                                 else "Unexpected: REVOKE it (not an access role; roles.sql's proof block raises "
                                      "-20011)"),
                      "MEANS": "Not an access role: anyone holding it can open the app."})
    return {"status": status, "rows": len(records), "present": present, "missing": missing,
            "missing_rerun": missing_rerun, "missing_pending": missing_pending, "ahead": ahead,
            "unexpected": unexpected, "owners": tuple(sorted(owners)), "table": table}
