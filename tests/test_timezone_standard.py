"""Timezone standard guard (see the timezone-consistency audit, 2026-09-21).

OVERWATCH runs in the Snowflake ACCOUNT default TIMEZONE, which IS America/Chicago
(verified 2026-09-21: SHOW PARAMETERS ... IN ACCOUNT -> value=America/Chicago,
level=ACCOUNT). The deployed Streamlit-in-Snowflake app CANNOT ALTER SESSION, so
that account default is the ONLY lever keeping the app's clock Central: every
session-tz read renders Central only because the account is Central.

These locks stop a future edit from silently re-anchoring the app's canonical
timezone, and keep the Central-PINNED SQL helpers (the account-correct escape hatch
new builders must use) intact. They do NOT change app behavior — the app is already
consistent; this is the "new builders pin Central" standard, enforced.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_account_timezone_is_america_chicago():
    # the app's canonical zone. If this ever changes, it must be a conscious decision
    # (the whole app clock + account_today() + the Central-pinned SQL helpers key off it).
    from app.logic.formulas import ACCOUNT_TIMEZONE
    assert ACCOUNT_TIMEZONE == "America/Chicago"


def test_account_date_helpers_pin_central_in_sql():
    # the account-correct escape hatch a new builder must use for a date boundary that
    # has to be right regardless of the session zone — these MUST stay Central-pinned
    # via CONVERT_TIMEZONE and must NOT fall back to session-tz CURRENT_DATE().
    from app.data.common import account_month_start_sql, account_today_sql
    today, month = account_today_sql(), account_month_start_sql()
    assert "CONVERT_TIMEZONE('America/Chicago'" in today
    assert "CONVERT_TIMEZONE('America/Chicago'" in month
    assert "CURRENT_DATE()" not in today
    assert "CURRENT_DATE()" not in month


def test_python_account_clock_is_tz_aware():
    # account_now()/account_today() resolve "now"/"today" in the account zone, not the
    # naive server (UTC) clock — the Python side of the same Central anchor.
    from app.logic import formulas
    src = inspect.getsource(formulas.account_now)
    assert "ZoneInfo(ACCOUNT_TIMEZONE)" in src


def test_timezone_standard_is_documented():
    # the standard (and the WHY — the SiS ALTER SESSION no-op reliance) is on record in
    # the data-layer conventions, where a builder author will see it.
    common = (_ROOT / "app" / "data" / "common.py").read_text(encoding="utf-8")
    assert "TIMEZONE STANDARD" in common
    assert "account_today_sql" in common and "CONVERT_TIMEZONE('America/Chicago'" in common
    assert "CANNOT ALTER SESSION" in common


# R2-052 (v4.609 / V167): the Cortex Code views' USAGE_TIME is TIMESTAMP_TZ, and ::DATE / ::TIMESTAMP_NTZ on a
# TIMESTAMP_TZ read the value's OWN stored offset, not the session zone -- the one class of timestamp the
# account-default rule above does not cover. A day key or wall-clock stamp off it converts to Central first.
_TZ_BARE = ("USAGE_TIME::DATE", "USAGE_TIME)::TIMESTAMP_NTZ")
# The pre-V167 loader mirror: the Admin AI recon must key days exactly as the deployed loader does, so its
# unstamped branch keeps the bare cast until has_migration(167) flips it (integration: central_days).
_TZ_BARE_ALLOWED = {("mart_sql.py", "mart_vs_live_ai_recon")}


def _enclosing_functions(path: Path) -> dict[int, str]:
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                lines.setdefault(ln, node.name)
    return lines


def test_timestamp_tz_day_keys_convert_to_central_in_app_data():
    hits = []
    for path in sorted((_ROOT / "app" / "data").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(b in text for b in _TZ_BARE):
            continue
        owners = _enclosing_functions(path)
        for i, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):          # prose about the rule, not SQL
                continue
            for bare in _TZ_BARE:
                for m in re.finditer(re.escape(bare), line):
                    if "CONVERT_TIMEZONE(" in line[:m.start()]:
                        continue
                    if (path.name, owners.get(i, "")) not in _TZ_BARE_ALLOWED:
                        hits.append(f"{path.name}:{i}: {line.strip()}")
    assert not hits, f"bare TIMESTAMP_TZ day key / NTZ cast (convert to Central first): {hits}"
    from app.data import cortex_sql
    assert cortex_sql.cortex_code_daily(30).count("CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE") == 1


def test_latest_marts_loader_keys_cortex_code_days_in_central():
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    body = _latest_proc_bodies()["SP_LOAD_MARTS_V27"]
    assert "CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY" in body
    assert "CONVERT_TIMEZONE('America/Chicago', MIN(c.USAGE_TIME))::TIMESTAMP_NTZ AS FIRST_TS" in body
    assert "CONVERT_TIMEZONE('America/Chicago', MAX(c.USAGE_TIME))::TIMESTAMP_NTZ AS LAST_TS" in body
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("--"))
    for bare in ("c.USAGE_TIME::DATE", "MIN(c.USAGE_TIME)::TIMESTAMP_NTZ", "MAX(c.USAGE_TIME)::TIMESTAMP_NTZ"):
        assert bare not in code.replace(f"CONVERT_TIMEZONE('America/Chicago', {bare}", ""), bare


def test_timezone_standard_names_the_timestamp_tz_rule():
    common = (_ROOT / "app" / "data" / "common.py").read_text(encoding="utf-8")
    assert "A TIMESTAMP_TZ column" in common and "carries its OWN offset" in common
    assert "TIMESTAMP_TZ value is NOT session-resolved" in common
