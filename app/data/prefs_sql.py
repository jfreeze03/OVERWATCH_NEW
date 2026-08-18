"""Per-user preference statements (USER_PREFS, V013).

Rows are always scoped server-side to the viewer identity (identity_sql():
st.user in SiS, CURRENT_USER() fallback — r27 #4), so no user input ever
selects whose prefs are read or written.
"""

from __future__ import annotations

import re

from app.config import core_object
from app.core.identity import identity_sql

# Offered display timezones; 'Account' means render as stored (account time).
DISPLAY_TIMEZONES = ("Account (America/Chicago)", "America/New_York",
                     "America/Los_Angeles", "UTC", "Europe/London")
VIEW_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,40}$")


def user_prefs() -> str:
    return f"""
SELECT PREF_KEY, PREF_VALUE, UPDATED_AT
FROM {core_object("USER_PREFS")}
WHERE USER_NAME = {identity_sql()}
ORDER BY PREF_KEY
"""
