"""#33: Admin stops calling 300 s "the default" statement timeout.

300 s is what V002 SET on the app warehouse at install (CREATE WAREHOUSE ... STATEMENT_TIMEOUT_IN_SECONDS =
300 plus an explicit ALTER). Snowflake's own default, when neither the warehouse nor the account sets
it, is 172800 s (48h). An empty SHOW PARAMETERS level means exactly that case, so it reads
"Snowflake default", not "ACCOUNT (default)". The V002 value is derived from the migration, never
hand-pinned.

v4.603 (#33 D2): nor is 300 s the value IN FORCE. On this account WH_ALFA_ADMIN's timeout cancels fire at
1800 s (W5b 2026-09-29; V139's header records WH 1800s), so no text may present V002's value as current
("V002 sets 5m there", "If V002 applied, 5m is set", "the app warehouse runs at 300s", "the real wall is the
300 s ..."), and a failed probe read of the value renders 'unavailable' with its error, not a guess.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_RETIRED_WORDING = (
    "300s default", "300s account default", "300s by default", "(or the account; 300s",
    "that 300s wall", "300s warehouse default", '"ACCOUNT (default)"',
    # v4.603 (#33 D2): V002's install-time 300 s stated as the value in force
    "runs at 300s", "the real wall is the 300 s", "that 300 s wall", "If V002 applied",
    "V002 sets {humanize_duration", "300 s STATEMENT_TIMEOUT_IN_SECONDS V002 sets",
)
_SCANNED = ("app/ui/pages/admin.py", "app/core/session.py", "app/ui/pages/alerts.py")


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Join wrapped lines (and continued `#` comments) so a phrase split across lines still matches."""
    return re.sub(r"[ \t]*\n[ \t]*#?[ \t]*", " ", text)


def test_the_300s_default_wording_is_gone():
    for rel in _SCANNED:
        flat = _flat(_src(rel))
        for phrase in _RETIRED_WORDING:
            assert phrase not in flat, f"{rel}: still says {phrase!r}"


def test_flattening_catches_a_wrapped_phrase():
    # negative control: the pre-#33 session.py comment wrapped "300s warehouse" / "# default"
    assert "300s warehouse default" in _flat("the real wall is the 300s warehouse\n# default), so")
    assert "300s by default" in _flat("STATEMENT_TIMEOUT_IN_SECONDS — 300s by\n    default — which")


def test_timeout_constants_are_derived_and_humanized():
    from app.logic.formulas import humanize_duration
    from app.ui.pages import admin
    v002 = _src("snowflake/migrations/V002__facts.sql")
    m = re.search(r"ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = (\d+);", v002)
    assert m, "V002 no longer sets the app warehouse timeout explicitly"
    assert int(m.group(1)) == admin._V002_APP_WH_TIMEOUT_S      # derived from V002, not hand-pinned
    assert admin._SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S == 172_800
    assert humanize_duration(admin._SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S, "s") == "48h"


def test_performance_tab_names_the_real_levels():
    adm = _src("app/ui/pages/admin.py")
    # v4.603 (#33 D2) moved this lock: the ceiling panel is its own function, and the failed-read fallback no
    # longer names V002's value (it guessed "If V002 applied, 5m is set", false on this account), so V002 is
    # named ONCE, as the install-time value, in the panel help
    perf = adm.split("def _stmt_timeout_ceiling(", 1)[1].split("\ndef ", 1)[0]
    assert "_stmt_timeout_ceiling()" in adm.split("def _performance_tab(", 1)[1].split("\ndef ", 1)[0]
    assert '"Snowflake default"' in perf
    assert "humanize_duration(_V002_APP_WH_TIMEOUT_S, 's')} at install; the value in force is read below." in perf
    assert "humanize_duration(_SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S, 's')" in perf
    assert perf.count("_V002_APP_WH_TIMEOUT_S") == 1 and perf.count("_SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S") >= 2
    assert 'empty_state("unavailable"' in perf and "detail=_to.error" in perf


def _render_ceiling(monkeypatch, result, account=None):
    """admin._stmt_timeout_ceiling with fakes: ``result`` is the warehouse SHOW PARAMETERS read, ``account``
    the account one (default: a failed read)."""
    from app.ui.pages import admin
    seen: dict = {"empty": [], "kpis": [], "tables": [], "help": [], "runs": []}
    account = account if account is not None else _result(None, ok=False, error="acct boom", error_kind="other")

    def fake_run(sql, *_a, **kw):
        seen["runs"].append((sql, kw))
        return account if sql.rstrip().endswith("IN ACCOUNT") else result

    monkeypatch.setattr(admin, "run", fake_run)
    monkeypatch.setattr(admin, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "panel_help", lambda text, *_a, **_k: seen["help"].append(text))
    monkeypatch.setattr(admin, "kpi_row", lambda items, *_a, **_k: seen["kpis"].append(items))
    monkeypatch.setattr(admin, "styled_table", lambda df, *_a, **_k: seen["tables"].append(df))
    monkeypatch.setattr(admin, "empty_state", lambda kind, msg, *_a, **k: seen["empty"].append(
        (kind, msg, k.get("detail"))))
    admin._stmt_timeout_ceiling()
    return seen


def _result(df, ok=True, error="", error_kind=""):
    from types import SimpleNamespace

    import pandas as pd
    frame = pd.DataFrame() if df is None else df
    return SimpleNamespace(ok=ok, empty=frame.empty, df=frame, error=error,
                           error_kind=error_kind if not ok else "",
                           usable=lambda: ok and not frame.empty)


def test_a_failed_ceiling_read_is_unavailable_with_its_error(monkeypatch):
    seen = _render_ceiling(monkeypatch, _result(None, ok=False, error="Insufficient privileges on WH_ALFA_ADMIN",
                                                error_kind="other"))
    assert seen["kpis"] == [] and seen["tables"] == []
    assert len(seen["empty"]) == 1
    kind, msg, detail = seen["empty"][0]
    assert kind == "unavailable" and detail == "Insufficient privileges on WH_ALFA_ADMIN"
    assert "the ceiling in force is unknown" in msg
    assert "V002" not in msg and "5m" not in msg                       # never a guess at the value
    assert "V002 set 5m at install; the value in force is read below." in seen["help"][0]


def _row(value, level):
    import pandas as pd
    return pd.DataFrame([{"key": "STATEMENT_TIMEOUT_IN_SECONDS", "value": value, "default": "172800",
                          "level": level}])


_ACCOUNT_6H = ("21600", "ACCOUNT")          # the account value on this account (probe answers W5)


def _tile(monkeypatch, wh, account=None):
    """The ceiling tile for a warehouse row (value, level) and an account row (value, level), or a failed
    account read when ``account`` is None."""
    acct = _result(_row(*account)) if account is not None else None
    seen = _render_ceiling(monkeypatch, _result(_row(*wh)), acct)
    return seen["kpis"][0][0], seen


def test_the_ceiling_kpi_shows_the_value_in_force(monkeypatch):
    kpi, seen = _tile(monkeypatch, ("1800", "WAREHOUSE"), _ACCOUNT_6H)
    assert (kpi["value"], kpi["delta"]) == ("30m", "set at: WAREHOUSE") and seen["empty"] == []
    # review R2-5: the account value is read too (the posture panel's metadata-tier probe read, same SQL)
    from app.data import ops_sql
    ((_sql, kw),) = [(q, k) for q, k in seen["runs"] if q == ops_sql.account_stmt_timeout_sql()]
    assert kw["tier"] == "metadata" and kw["probe"] is True and kw["max_rows"] == 0
    # ok but no row: a quiet note, never 'unavailable' and never a guessed value
    empty = _render_ceiling(monkeypatch, _result(None))
    assert [k for k, _m, _d in empty["empty"]] == ["no_data_yet"] and "5m" not in empty["empty"][0][1]


_SEVEN_DAY = "which Snowflake enforces as the 7-day maximum"


def test_an_account_level_zero_reads_as_the_seven_day_max(monkeypatch):
    """Review R1-8/R2-5: STATEMENT_TIMEOUT_IN_SECONDS = 0 is Snowflake's 7-day maximum, never '0s' (which reads
    as 'every read is killed at once'). A 0 the warehouse INHERITS from the account is 0 on the session side
    too, so there (and only when both sides are 0) the effective ceiling really is 168h."""
    for account in (("0", "ACCOUNT"), None):                   # read, or derived from the warehouse row
        kpi, seen = _tile(monkeypatch, ("0", "ACCOUNT"), account)
        assert kpi["value"] == "168h" and kpi["value"] != "0s", account
        assert kpi["delta"] == "set at: ACCOUNT; 0 = 7-day max", account
        assert _SEVEN_DAY in kpi["help"], account
        assert str(seen["tables"][0].iloc[0]["value"]) == "0"          # the raw row is shown verbatim
    kpi, _ = _tile(monkeypatch, ("0", "WAREHOUSE"), ("0", "ACCOUNT"))
    assert (kpi["value"], kpi["delta"]) == ("168h", "set at: WAREHOUSE (0 = no warehouse limit); 0 = 7-day max")


def test_a_warehouse_level_zero_is_capped_by_the_account_value(monkeypatch):
    """Review R2-5: a WAREHOUSE-level 0 only lifts the warehouse's own limit; reads are still capped by the
    session side (the account's 6h here). The tile used to say 168h with '0 = 7-day max' for this case."""
    kpi, _ = _tile(monkeypatch, ("0", "WAREHOUSE"), _ACCOUNT_6H)
    assert kpi["value"] == "6h"
    assert kpi["delta"] == "set at: WAREHOUSE (0 = no warehouse limit); capped by the account value"
    assert "7-day" not in kpi["delta"] and _SEVEN_DAY not in kpi["help"]
    assert "The warehouse's own value is 0 (no warehouse limit), so the account value (6h) caps reads." in kpi["help"]
    # the account sets none: Snowflake's 172800 s default caps reads (48h), named as such
    kpi, _ = _tile(monkeypatch, ("0", "WAREHOUSE"), ("172800", ""))
    assert (kpi["value"], kpi["delta"]) == (
        "48h", "set at: WAREHOUSE (0 = no warehouse limit); capped by Snowflake's default (the account sets none)")


def test_a_warehouse_value_above_the_account_value_is_capped_by_it(monkeypatch):
    """Review R2-5: the same rule for a non-zero warehouse value above the account's (8h vs 6h): 6h caps reads."""
    kpi, _ = _tile(monkeypatch, ("28800", "WAREHOUSE"), _ACCOUNT_6H)
    assert (kpi["value"], kpi["delta"]) == ("6h", "set at: WAREHOUSE; capped by the account value")
    assert "The warehouse's own value is 8h, so the account value (6h) caps reads." in kpi["help"]
    # a lower warehouse value wins with no cap note; an unparseable value is the dash, never '0s'
    k7, _ = _tile(monkeypatch, ("7200", "WAREHOUSE"), _ACCOUNT_6H)
    assert (k7["value"], k7["delta"]) == ("2h", "set at: WAREHOUSE") and "7-day" not in k7["help"]
    kbad, seen = _tile(monkeypatch, ("n/a", "WAREHOUSE"), _ACCOUNT_6H)
    assert kbad["value"] == "—" and len(seen["runs"]) == 1          # nothing to compare: no account read


def test_an_unread_account_value_qualifies_the_warehouse_value(monkeypatch):
    """Review R2-5: when the account SHOW fails and the warehouse sets its own value, nothing says what the
    session side is: the tile shows the warehouse's own value, qualified, and never claims the 7-day maximum
    is the real ceiling."""
    kpi, _ = _tile(monkeypatch, ("0", "WAREHOUSE"))
    assert kpi["value"] == "168h"
    assert kpi["delta"] == ("set at: WAREHOUSE (0 = no warehouse limit); the session/account value, if lower, "
                            "caps reads")
    assert "0 = 7-day max" not in kpi["delta"] and _SEVEN_DAY not in kpi["help"]
    assert "The account value could not be read, so this is the warehouse's own value" in kpi["help"]
    kpi, _ = _tile(monkeypatch, ("1800", "WAREHOUSE"))
    assert (kpi["value"], kpi["delta"]) == ("30m", "set at: WAREHOUSE; the session/account value, if lower, "
                                                   "caps reads")


def test_a_failed_ceiling_read_names_a_cause_only_when_the_error_says_so(monkeypatch):
    """Review R1-14: the app warehouse is the app's own query_warehouse, so its owner role holds USAGE on it; a
    timeout or other failure is not a privilege gap. Only Snowflake's 'does not exist or not authorized'
    (error_kind 'absent') names a cause, worded as that error is (missing OR no privilege)."""
    for kind in ("timeout", "other", "missing_column", "unknown_function"):
        seen = _render_ceiling(monkeypatch, _result(None, ok=False, error=f"boom ({kind})", error_kind=kind))
        ((state, msg, detail),) = seen["empty"]
        assert state == "unavailable" and detail == f"boom ({kind})", kind
        assert msg == "Could not read STATEMENT_TIMEOUT_IN_SECONDS on WH_ALFA_ADMIN, so the ceiling in force is unknown."
        assert "MONITOR" not in msg and "USAGE" not in msg and "privilege" not in msg, kind
    seen = _render_ceiling(monkeypatch, _result(None, ok=False, error="Warehouse 'WH_ALFA_ADMIN' does not exist "
                                                "or not authorized.", error_kind="absent"))
    ((state, msg, _detail),) = seen["empty"]
    assert state == "unavailable" and "the ceiling in force is unknown" in msg
    assert "(the warehouse is missing, or the app's owner role has no privilege on it)" in msg
    assert "MONITOR/USAGE" not in msg
