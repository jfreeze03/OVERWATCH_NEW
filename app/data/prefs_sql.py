"""Per-user preference statements (USER_PREFS, V013).

Rows are always scoped server-side to the viewer identity (identity_sql():
st.user in SiS, CURRENT_USER() fallback — r27 #4), so no user input ever
selects whose prefs are read or written.
"""

from __future__ import annotations

import re

from app.config import core_object
from app.core.identity import identity_sql
from app.core.sqlsafe import sql_literal

_KEY_RE = re.compile(r"^(DEFAULT_VIEW|DISPLAY_TZ|VIEW:[A-Za-z0-9 _\-]{1,40})$")

# Offered display timezones; 'Account' means render as stored (account time).
DISPLAY_TIMEZONES = ("Account (America/Chicago)", "America/New_York",
                     "America/Los_Angeles", "UTC", "Europe/London")
VIEW_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,40}$")


def _valid_key(key: str) -> str:
    key = str(key or "").strip()
    if not _KEY_RE.match(key):
        raise ValueError(f"Invalid pref key: {key!r}")
    return key


def user_prefs() -> str:
    return f"""
SELECT PREF_KEY, PREF_VALUE, UPDATED_AT
FROM {core_object("USER_PREFS")}
WHERE USER_NAME = {identity_sql()}
ORDER BY PREF_KEY
"""


def upsert_pref_sql(key: str, value_json: str) -> str:
    key = _valid_key(key)
    value = str(value_json or "")[:4000]
    return (
        f"MERGE INTO {core_object('USER_PREFS')} t "
        f"USING (SELECT {identity_sql()} AS U, {sql_literal(key)} AS K) s "
        "ON t.USER_NAME = s.U AND t.PREF_KEY = s.K "
        f"WHEN MATCHED THEN UPDATE SET PREF_VALUE = {sql_literal(value)}, UPDATED_AT = CURRENT_TIMESTAMP() "
        f"WHEN NOT MATCHED THEN INSERT (USER_NAME, PREF_KEY, PREF_VALUE) "
        f"VALUES (s.U, s.K, {sql_literal(value)});"
    )


def delete_pref_sql(key: str) -> str:
    key = _valid_key(key)
    return (
        f"DELETE FROM {core_object('USER_PREFS')} "
        f"WHERE USER_NAME = {identity_sql()} AND PREF_KEY = {sql_literal(key)};"
    )
