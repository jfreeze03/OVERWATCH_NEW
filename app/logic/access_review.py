"""Admin ▸ Access (v4.610.0, owner decision 2026-10-05): the pure readers behind the tab.

Two different questions, answered in two different places:
  * who can OPEN the app is Snowflake's USAGE on the Streamlit. Exactly config.APP_ACCESS_ROLES may hold it;
    snowflake/roles.sql's -20011/-20012 proof block pins that set, and app_grant_review applies the same
    rule to a SHOW GRANTS ON STREAMLIT answer;
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
    ADMIN_ACCESS_ROLE,
    APP_ACCESS_ROLES,
    PAGES_BY_PROFILE,
    VIEW_ACCESS_ROLE,
    VIEWER_UNKNOWN_PROFILE,
)
from app.logic.formulas import ACCOUNT_TIMEZONE, humanize_duration

#: The roles that must hold USAGE on the app, and the only ones that may (roles.sql's proof block).
EXPECTED_APP_GRANTEES: tuple[str, ...] = tuple(APP_ACCESS_ROLES)

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

#: What holding USAGE on the app means for each expected role (the grants table's MEANS column).
ROLE_MEANING: dict[str, str] = {
    "SNOW_ACCOUNTADMINS": ("Opens the app and owns it: every viewer's SQL runs with this role's rights. A holder "
                           "is an admin only via OPERATOR_USERS or a direct admin-role grant."),
    "SNOW_SYSADMINS": ("Opens the app. A holder is an admin only via OPERATOR_USERS or a direct admin-role "
                       "grant; otherwise view-only."),
    ADMIN_ACCESS_ROLE: ("Opens the app. A DIRECT user member is an OVERWATCH admin: every page and every "
                        "in-app change. A role granted this role is not expanded."),
    VIEW_ACCESS_ROLE: f"Opens the app. View-only: {_VIEW_PAGES}, no changes.",
}


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
                "headline": (f"The admin-role lookup failed (SHOW GRANTS OF ROLE {role}). Viewers not on "
                             "OPERATOR_USERS are read-only until it recovers.")}
    if status == "unverified":
        return {"state": "unavailable", "detail": detail,
                "headline": (f"The admin-role lookup is unverified: SHOW GRANTS OF ROLE {role} listed no USER "
                             "grantee. That is a privilege gap or an empty role, never read as 'no members'. "
                             "Viewers not on OPERATOR_USERS are read-only until it lists them.")}
    return {"state": "not_checked", "detail": "",
            "headline": ("The admin-role lookup runs only on Streamlit-in-Snowflake, as the app owner. Off "
                         "SiS the role -> profile map decides access.")}


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
    """Compare a SHOW GRANTS ON STREAMLIT answer with the roles that must (and alone may) open the app.

    The rule is roles.sql's proof block: a USAGE row is allowed only when granted_to = 'ROLE' and the
    grantee is one of ``expected`` (so a database role, application role or share spelled like an access role
    is still unexpected), and every expected role must hold USAGE. Other privileges (OWNERSHIP) never count
    either way; their grantees are reported as ``owners``.

    Returns {status: ok | drift | empty, rows, present, missing, unexpected, owners, table}. An empty answer
    is 'empty' (the owner always sees its own OWNERSHIP row, so nothing at all means the read could not see
    the app): unverified, never clean. ``table`` lists every expected role and every unexpected grantee with
    EXPECTED / HAS_USAGE / STATUS and what the role MEANS."""
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
        table.append({"GRANTEE": role, "KIND": "ROLE", "EXPECTED": "Yes", "HAS_USAGE": "Yes" if ok else "No",
                      "STATUS": "OK" if ok else "Missing: re-run snowflake/roles.sql's Streamlit grants",
                      "MEANS": ROLE_MEANING.get(role, "")})
    for kind, name in sorted({(k, n) for k, n in usage if not (k == "ROLE" and n in expected)}):
        table.append({"GRANTEE": name, "KIND": kind or "?", "EXPECTED": "No", "HAS_USAGE": "Yes",
                      "STATUS": "Unexpected: REVOKE it (roles.sql's proof block raises -20011)",
                      "MEANS": "Not an access role: anyone holding it can open the app."})
    return {"status": status, "rows": len(records), "present": present, "missing": missing,
            "unexpected": unexpected, "owners": tuple(sorted(owners)), "table": table}
