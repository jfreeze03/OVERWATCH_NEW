"""Resolve a Snowflake login (USER_NAME) to a person's name for display.

"First Last" when ACCOUNT_USAGE.USERS carries them; the login itself when it does
not (service accounts, dropped users) — never blank, never invented. This is the
Cortex AI attribution rule (app/logic/cortex.py), generalized so every surface that
shows a USER_NAME can show WHO the person is.
"""
from __future__ import annotations

import pandas as pd


def display_name_map(directory: pd.DataFrame, user_col: str = "USER_NAME") -> dict[str, str]:
    """Build {UPPER(login): "First Last"} from the USERS directory frame.

    Only logins that resolve to a non-empty first/last are included; everything
    else falls back to the login at lookup time (see ``resolve_display``).
    """
    if (directory is None or directory.empty or user_col not in directory.columns
            or "FIRST_NAME" not in directory.columns or "LAST_NAME" not in directory.columns):
        return {}
    full = (directory["FIRST_NAME"].fillna("").astype(str).str.strip() + " "
            + directory["LAST_NAME"].fillna("").astype(str).str.strip()).str.strip()
    keys = directory[user_col].fillna("").astype(str).str.strip().str.upper()
    return {k: v for k, v in zip(keys, full, strict=False) if k and v}


def resolve_display(user_name: object, name_map: dict[str, str]) -> str:
    """"First Last" if the login is known, else the login string (never blank)."""
    login = str(user_name or "").strip()
    return name_map.get(login.upper(), login)


def attach_display_name(df: pd.DataFrame, name_map: dict[str, str],
                        user_col: str = "USER_NAME", display_col: str = "USER") -> pd.DataFrame:
    """Return a copy of ``df`` with ``display_col`` = "First Last" (login fallback).

    The raw ``user_col`` is preserved so a viewer sees BOTH the login and who it is.
    No-op when the frame has no ``user_col``.
    """
    if df is None or df.empty or user_col not in df.columns:
        return df
    out = df.copy()
    values = out[user_col].map(lambda u: resolve_display(u, name_map))
    if display_col in out.columns:
        out[display_col] = values
    else:
        out.insert(out.columns.get_loc(user_col) + 1, display_col, values)   # next to the login
    return out
