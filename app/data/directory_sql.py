"""User-directory reader: login (USER_NAME) -> First/Last, from ACCOUNT_USAGE.USERS.

One small, account-global read (metadata tier, shared cache entry) that every page
uses to show a person's name next to the cryptic login — the Cortex AI attribution
treatment (app/data/cortex_sql.py + app/logic/cortex.py), generalized. App-side by
design: it resolves for BOTH live-query and mart-backed frames without touching any
loader SQL, since the marts store the login string and not USER_ID.
"""
from __future__ import annotations


def user_directory() -> str:
    """Current (non-deleted) users keyed by login NAME, deduped to the latest row.

    NAME can repeat in ACCOUNT_USAGE.USERS when a login is dropped and recreated;
    DELETED_ON IS NULL keeps live users and QUALIFY picks the newest per NAME.
    """
    return """
SELECT NAME AS USER_NAME, FIRST_NAME, LAST_NAME, EMAIL
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS
WHERE DELETED_ON IS NULL AND NAME IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY NAME ORDER BY CREATED_ON DESC NULLS LAST) = 1
"""
