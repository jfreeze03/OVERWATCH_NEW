"""Locks for the login -> "First Last" display resolver (shown everywhere USER_NAME
appears, generalizing the Cortex AI attribution treatment)."""
import pandas as pd

from app.data import directory_sql
from app.logic.directory import (
    attach_display_name,
    attach_name_parts,
    display_name_map,
    name_parts_map,
    resolve_display,
)


def _dir() -> pd.DataFrame:
    return pd.DataFrame({
        "USER_NAME": ["SSLONSKY", "svc_etl", "KEBARR1", "NONAME"],
        "FIRST_NAME": ["Steve", "", "Kevin", None],
        "LAST_NAME": ["Slonsky", "", None, None],
        "EMAIL": ["s@x.com", None, "k@x.com", None],
    })


def test_display_name_map_only_keeps_resolvable_logins():
    m = display_name_map(_dir())
    assert m == {"SSLONSKY": "Steve Slonsky", "KEBARR1": "Kevin"}   # case-folded keys
    # blank/absent names are NOT in the map (they fall back to the login on lookup)
    assert "SVC_ETL" not in m and "NONAME" not in m
    assert display_name_map(pd.DataFrame()) == {}                   # empty is safe
    assert display_name_map(pd.DataFrame({"USER_NAME": ["A"]})) == {}  # no name cols -> empty


def test_resolve_display_falls_back_to_login_never_blank():
    m = display_name_map(_dir())
    assert resolve_display("sslonsky", m) == "Steve Slonsky"        # case-insensitive
    assert resolve_display("SVC_ETL", m) == "SVC_ETL"              # service acct -> login
    assert resolve_display("NOBODY", m) == "NOBODY"               # unknown -> login
    assert resolve_display(None, m) == ""                          # never crashes
    assert resolve_display("x", {}) == "x"                         # empty map -> login


def test_attach_display_name_adds_column_and_keeps_login():
    m = display_name_map(_dir())
    df = pd.DataFrame({"USER_NAME": ["SSLONSKY", "svc_etl"], "CREDITS": [10, 5]})
    out = attach_display_name(df, m)
    assert list(out["USER"]) == ["Steve Slonsky", "svc_etl"]       # resolved + fallback
    assert list(out["USER_NAME"]) == ["SSLONSKY", "svc_etl"]       # login preserved
    # no-op when there is no user column
    plain = pd.DataFrame({"X": [1]})
    assert attach_display_name(plain, m).equals(plain)


def test_attach_name_parts_preserves_exact_first_and_last_columns():
    parts = name_parts_map(_dir())
    assert parts == {
        "SSLONSKY": ("Steve", "Slonsky"),
        "KEBARR1": ("Kevin", ""),
    }
    df = pd.DataFrame({"USER_NAME": ["SSLONSKY", "svc_etl"], "CREDITS": [10, 5]})
    out = attach_name_parts(df, parts)
    assert list(out.columns[:4]) == ["USER_NAME", "FIRST_NAME", "LAST_NAME", "CREDITS"]
    assert out.iloc[0]["FIRST_NAME"] == "Steve"
    assert out.iloc[0]["LAST_NAME"] == "Slonsky"
    assert pd.isna(out.iloc[1]["FIRST_NAME"]) and pd.isna(out.iloc[1]["LAST_NAME"])


def test_user_directory_sql_shape():
    sql = directory_sql.user_directory()
    assert "SNOWFLAKE.ACCOUNT_USAGE.USERS" in sql
    assert "NAME AS USER_NAME" in sql and "FIRST_NAME" in sql and "LAST_NAME" in sql
    assert "DELETED_ON IS NULL" in sql                             # current users only
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY NAME" in sql   # deduped by login


def test_components_expose_the_reusable_helpers():
    from pathlib import Path
    cmp = Path(__file__).resolve().parents[1] / "app" / "ui" / "components.py"
    src = cmp.read_text(encoding="utf-8")
    assert "def user_display_map(page: str)" in src
    assert "def user_name_parts_map(page: str)" in src
    assert "def with_user_names(" in src
    assert "def with_user_name_parts(" in src
    assert 'tier="metadata"' in src and 'key="user_directory"' in src  # one shared cached read


def test_login_surfaces_wire_the_resolver():
    """Every page that shows a Snowflake-login column resolves it to a name (no
    login column renders raw). Guards against a refactor silently dropping it."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "ui"
    login_cols = ("USER_NAME", "GRANTEE_NAME", "ACK_BY", "CHANGED_BY",
                  "DECLARED_BY", "LINKED_BY", "CREATED_BY", "UPDATED_BY", "GRANTED_BY")
    for path in list(root.glob("pages/*.py")) + list(root.glob("pages/cost_parts/*.py")) + [root / "components.py"]:
        src = path.read_text(encoding="utf-8")
        if any(c in src for c in login_cols):
            assert ("with_user_names" in src or "resolve_display" in src), f"{path.name} shows a login but never resolves it"
    # the shared blast-radius component (Alerts + Optimize, pre-cancel/suspend) resolves
    # (_display is df minus the window-total helper columns — bug-hunt 2026-08-30)
    assert "with_user_names(_display, page)" in (root / "components.py").read_text(encoding="utf-8")
