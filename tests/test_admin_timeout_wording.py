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


def _render_ceiling(monkeypatch, result):
    """admin._stmt_timeout_ceiling with fakes: ``result`` is the SHOW PARAMETERS read."""
    from app.ui.pages import admin
    seen: dict = {"empty": [], "kpis": [], "tables": [], "help": []}
    monkeypatch.setattr(admin, "run", lambda *_a, **_k: result)
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


def test_the_ceiling_kpi_shows_the_value_in_force(monkeypatch):
    import pandas as pd
    row = pd.DataFrame([{"key": "STATEMENT_TIMEOUT_IN_SECONDS", "value": "1800", "default": "172800",
                         "level": "WAREHOUSE"}])
    seen = _render_ceiling(monkeypatch, _result(row))
    kpi = seen["kpis"][0][0]
    assert (kpi["value"], kpi["delta"]) == ("30m", "set at: WAREHOUSE") and seen["empty"] == []
    # ok but no row: a quiet note, never 'unavailable' and never a guessed value
    empty = _render_ceiling(monkeypatch, _result(None))
    assert [k for k, _m, _d in empty["empty"]] == ["no_data_yet"] and "5m" not in empty["empty"][0][1]


def test_a_warehouse_value_of_zero_reads_as_the_seven_day_max(monkeypatch):
    """Review R1-8: STATEMENT_TIMEOUT_IN_SECONDS = 0 is Snowflake's 7-day maximum, so the 'value in force' tile
    shows the ENFORCED 168h and says why (stmt_timeout.enforced_s, the posture panel's account_value_kpi rule),
    never '0s' (which reads as 'every read is killed at once'). The raw 0 stays in the SHOW table."""
    import pandas as pd
    row = pd.DataFrame([{"key": "STATEMENT_TIMEOUT_IN_SECONDS", "value": "0", "default": "172800",
                         "level": "WAREHOUSE"}])
    seen = _render_ceiling(monkeypatch, _result(row))
    kpi = seen["kpis"][0][0]
    assert kpi["value"] == "168h" and kpi["value"] != "0s"
    assert kpi["delta"] == "set at: WAREHOUSE; 0 = 7-day max"
    assert "It is set to 0, which Snowflake enforces as the 7-day maximum." in kpi["help"]
    assert str(seen["tables"][0].iloc[0]["value"]) == "0"                # the raw row is shown verbatim
    # a non-zero value keeps its plain delta and help; an unparseable value is the dash, never '0s'
    row7 = row.assign(value="7200")
    k7 = _render_ceiling(monkeypatch, _result(row7))["kpis"][0][0]
    assert (k7["value"], k7["delta"]) == ("2h", "set at: WAREHOUSE") and "7-day" not in k7["help"]
    kbad = _render_ceiling(monkeypatch, _result(row.assign(value="n/a")))["kpis"][0][0]
    assert kbad["value"] == "—"


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
