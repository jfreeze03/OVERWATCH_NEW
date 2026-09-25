"""Next-Fifty #12a: the incident lifecycle the app now WRITES — Acknowledge, Mark mitigated,
the Close back-fill — and the metrics / cues that read it (MTTA, time to mitigate, ready to close).

Before #12 nothing wrote INCIDENTS.ACK_AT / OWNER / MITIGATED_AT, so the incident MTTA had been
dropped (ALC-2) and 'MITIGATED' existed only in predicates. These locks keep the writes
forward-only, injection-safe, allow-listed, domain-scoped, INC-1 pre-checked and latched, keep
MTTA an auto-declared-only human number, keep the ready count off the LIMIT-capped feed, and keep
the V154 caption schema-gated so a deploy that lands before the apply never overclaims.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from app.core import identity
from app.core.query import _domains_in, _statement_allowed
from app.core.result import QueryResult
from app.data import mart_sql
from app.ui.pages import control_room as cr

_ROOT = Path(__file__).resolve().parents[1]
_CR = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
_ALERTS = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


# ---------------------------------------------------------------------------
# The write SQL — forward-only guards, provenance, injection safety, allow-list, domain
# ---------------------------------------------------------------------------

def test_ack_sql_is_forward_only_and_never_restamps(monkeypatch):
    monkeypatch.setattr(identity, "viewer_name", lambda: "JDOE")
    sql = cr._incident_ack_sql("abc-123")
    assert sql.startswith("UPDATE DBA_MAINT_DB.OVERWATCH.INCIDENTS SET ")
    assert "SET ACK_AT = CURRENT_TIMESTAMP(), OWNER = COALESCE(OWNER, 'JDOE'), " in sql
    assert "WHERE INCIDENT_ID = 'abc-123' " in sql
    # a re-click or a second operator never re-stamps; RESOLVED rows never move
    assert sql.endswith("AND STATUS IN ('OPEN', 'MITIGATED') AND ACK_AT IS NULL;")
    assert "'RESOLVED'" not in sql and "MITIGATED_BY" not in sql


def test_owner_falls_back_to_current_user_outside_sis(monkeypatch):
    monkeypatch.setattr(identity, "viewer_name", lambda: "")
    assert "OWNER = COALESCE(OWNER, CURRENT_USER())" in cr._incident_ack_sql("i1")
    assert "OWNER = COALESCE(OWNER, CURRENT_USER())" in cr._incident_mitigate_sql("i1")


def test_mitigate_sql_moves_open_forward_and_backfills_first_response(monkeypatch):
    monkeypatch.setattr(identity, "viewer_name", lambda: "JDOE")
    sql = cr._incident_mitigate_sql("abc-123")
    assert "SET STATUS = 'MITIGATED', MITIGATED_AT = CURRENT_TIMESTAMP(), " in sql
    assert "ACK_AT = COALESCE(ACK_AT, CURRENT_TIMESTAMP()), " in sql      # first response, once
    assert "OWNER = COALESCE(OWNER, 'JDOE'), " in sql
    assert sql.endswith("WHERE INCIDENT_ID = 'abc-123' AND STATUS = 'OPEN';")
    # a human mitigate leaves MITIGATED_BY NULL (only V154's sweep stamps it); never closes
    assert "MITIGATED_BY" not in sql and "'RESOLVED'" not in sql


def test_close_backfills_first_response_but_the_bulk_reset_does_not(monkeypatch):
    monkeypatch.setattr(identity, "viewer_name", lambda: "JDOE")
    close = cr._incident_close_sql("abc-123", "deploy", "rolled back")
    assert "SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), " in close
    assert "ACK_AT = COALESCE(ACK_AT, CURRENT_TIMESTAMP()), " in close
    assert "OWNER = COALESCE(OWNER, 'JDOE'), " in close
    assert close.endswith("AND STATUS IN ('OPEN', 'MITIGATED');")      # still forward-only
    bulk = cr._clear_open_incidents_sql("ALL", "UNKNOWN", "reset")
    assert "ACK_AT" not in bulk and "OWNER" not in bulk                  # clearing a board is not a response


@pytest.mark.parametrize("builder", ["_incident_ack_sql", "_incident_mitigate_sql",
                                     "_incident_state_sql", "_incident_close_sql"])
def test_lifecycle_sql_is_injection_safe(builder, monkeypatch):
    monkeypatch.setattr(identity, "viewer_name", lambda: "O'Brien")
    fn = getattr(cr, builder)
    sql = fn("x'y", "UNKNOWN", "n") if builder == "_incident_close_sql" else fn("x'y")
    assert "INCIDENT_ID = 'x''y'" in sql
    if builder != "_incident_state_sql":
        assert "'O''Brien'" in sql


def test_writes_pass_the_allow_list_and_bump_only_the_incidents_domain():
    for sql in (cr._incident_ack_sql("i1"), cr._incident_mitigate_sql("i1")):
        ok, why = _statement_allowed(sql)
        assert ok, why
        assert _domains_in(sql) == ["incidents"]
    # the pre-check read lives in the same domain, so the post-write salt bump refreshes it
    state = cr._incident_state_sql("i1")
    assert state.startswith("SELECT STATUS, (ACK_AT IS NULL) AS UNACKED ")
    assert _domains_in(state) == ["incidents"]


# ---------------------------------------------------------------------------
# The drawer wiring — ordering, INC-1 pre-check, latches, no raw st.info
# ---------------------------------------------------------------------------

def test_lifecycle_controls_render_before_the_rca_and_the_close():
    call = "_incident_lifecycle_controls(oi.df.iloc[int(sel_i)], _iid, can_write=_is_op)"
    assert _CR.count(call) == 1
    assert _CR.index(call) < _CR.index("_auto_investigation(oi.df.iloc[int(sel_i)], company, rate)")
    assert _CR.index(call) < _CR.index('st.expander("Close this incident')


def test_each_lifecycle_write_is_prechecked_and_latched():
    body = _body(_CR, "_incident_lifecycle_controls")
    for kind, sql_var, event in (("ack", "ack_sql", "incident_ack"), ("mit", "mit_sql", "incident_mitigate")):
        assert body.count(f'write_gate_open(f"inc_{kind}_{{iid[:8]}}")') == 1, kind
        assert body.count(f'stamp_write(f"inc_{kind}_{{iid[:8]}}", ok)') == 1, kind
        ex = body.index(f"execute_statement({sql_var}")
        block_start = body.index(f'write_gate_open(f"inc_{kind}_{{iid[:8]}}")')
        # INC-1: the live-state read comes BEFORE the write, inside the same click block
        assert block_start < body.index("_incident_state_sql(iid)", block_start) < ex, kind
        assert ex < body.index(f'stamp_write(f"inc_{kind}_{{iid[:8]}}", ok)'), kind
        assert f'log_ui_event("{event}", page=_PAGE)' in body
    # a row that no longer exists is a no-op too (never a phantom 'acknowledged' / 'mitigated' toast)
    assert "gone = chk.ok and row0 is None" in body and "(chk.ok and chk.empty)" in body
    # Acknowledge is one click; Mark mitigated needs a typed confirm
    assert 'confirm_gate("MITIGATE", "Mark mitigated"' in body
    # the ready note is a context caption, not a raw st.info (tests/test_uiux_wave2_empties ceiling)
    assert not re.search(r"st\.(?:info|success)\(", body)
    # non-operators still see the note + provenance: the write gate returns AFTER both
    assert body.index("if not can_write:") > body.index('st.caption("Lifecycle: "')
    assert body.index("if not can_write:") > body.index("Ready to close")


def test_ready_count_comes_from_the_uncapped_metrics_row():
    assert 'inc_met.df.iloc[0].get("READY_TO_CLOSE_N")' in _CR
    ready_block = _CR.split("_ready_n = ", 1)[1].split("if oi.ok and oi.empty", 1)[0]
    assert "len(oi.df)" not in ready_block and "st.caption(" in ready_block


def test_only_the_control_room_reads_the_lifecycle_columns():
    assert _CR.count("mart_sql.open_incidents(50, company, lifecycle=True)") == 2   # batch + fallback
    brief = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "lifecycle=True" not in brief and "lifecycle=True" not in canary
    assert 'user_col="OWNER", display_col="Owner"' in _CR


def test_owner_nan_renders_the_em_dash():
    body = _body(_CR, "_incident_lifecycle_controls")
    assert "if pd.notna(owner_raw) else" in body and "{owner or '—'}" in body


# ---------------------------------------------------------------------------
# Readers — open_incidents(lifecycle) + incident_metrics
# ---------------------------------------------------------------------------

def test_open_incidents_default_stays_the_cheap_read():
    cheap = mart_sql.open_incidents(5, "ALFA")
    for col in ("ACK_AT", "OWNER", "MITIGATED_AT", "READY_TO_CLOSE", "inc_ready"):
        assert col not in cheap, col
    assert "(i.COMPANY = 'ALFA' OR UPPER(i.COMPANY) = 'ALL')" in cheap


def test_open_incidents_lifecycle_adds_the_columns_without_touching_the_frame():
    sqlglot = pytest.importorskip("sqlglot")
    oi = mart_sql.open_incidents(50, "ALFA", lifecycle=True)
    for col in ("i.ACK_AT", "i.OWNER", "i.MITIGATED_AT", "AS READY_TO_CLOSE", "AS MEMBERS"):
        assert col in oi, col
    assert "COALESCE(r.LIVE_MEMBERS = 0 AND r.LIVE_SUCCESSORS = 0, FALSE) AS READY_TO_CLOSE" in oi
    assert "LEFT JOIN inc_ready r ON r.INCIDENT_ID = i.INCIDENT_ID" in oi   # never drops a row
    assert "WHERE i.STATUS IN ('OPEN', 'MITIGATED') AND (i.COMPANY = 'ALFA'" in oi
    assert "LIMIT 50" in oi
    assert "COMPANY = '" not in mart_sql.open_incidents(50, lifecycle=True)   # ALL stays account-wide
    for sql in (oi, mart_sql.open_incidents(50, lifecycle=True)):
        sqlglot.parse_one(sql, dialect="snowflake")


def test_incident_metrics_restores_mtta_for_auto_declared_only():
    sqlglot = pytest.importorskip("sqlglot")
    met = mart_sql.incident_metrics(90, "ALFA")
    mtta = met.split("AS TTD_MIN,", 1)[1].split("AS MTTA_MIN", 1)[0]
    assert "MEDIAN(DATEDIFF('minute', DETECTED_AT, ACK_AT))" in mtta
    assert "ACK_AT >= DETECTED_AT" in mtta                                # no negative spans
    assert "DECLARED_BY = 'SP_INCIDENT_AUTODECLARE'" in mtta              # O-6: auto-declared only
    acked = met.split("AS MTTA_MIN,", 1)[1].split("AS ACKED_N", 1)[0]
    assert "DECLARED_BY = 'SP_INCIDENT_AUTODECLARE'" in acked and "ACK_AT >= DETECTED_AT" in acked
    mttm = met.split("AS ACKED_N,", 1)[1].split("AS MTTM_MIN", 1)[0]
    assert "DATEDIFF('minute', DETECTED_AT, MITIGATED_AT)" in mttm and "MITIGATED_AT >= DETECTED_AT" in mttm
    assert "ready.READY_N AS READY_TO_CLOSE_N" in met
    assert "FROM wn CROSS JOIN compression CROSS JOIN ready" in met
    # the ready count is company-scoped with the i.-qualified arm; the bare arm stays at 2
    assert "AND (i.COMPANY = 'ALFA' OR UPPER(i.COMPANY) = 'ALL')" in met
    assert met.count("(COMPANY = 'ALFA' OR UPPER(COMPANY) = 'ALL')") == 2
    assert "NULLIF((SELECT" not in met and "reopen" not in met
    sqlglot.parse_one(met, dialect="snowflake")
    sqlglot.parse_one(mart_sql.incident_metrics(90), dialect="snowflake")


def test_ready_cte_is_uncorrelated_and_open_only():
    cte = mart_sql._incident_ready_cte()
    assert cte.startswith("inc_ready AS (")
    assert "ri.STATUS IN ('OPEN', 'MITIGATED')" in cte
    assert "WHERE m.MEMBER_KIND = 'ALERT'" in cte
    assert "COUNT_IF(e.EVENT_ID IS NULL OR e.STATUS <> 'RESOLVED') AS LIVE_MEMBERS" in cte


def test_alerts_history_shows_mtta_and_time_to_mitigate():
    assert '"label": "MTTA (90d)"' in _ALERTS
    assert '"label": "Time to mitigate (90d)"' in _ALERTS
    assert '"label": "MTTR (90d)"' in _ALERTS                              # test_bughunt_round4 lock
    assert 'humanize_duration(im.get("MTTA_MIN"), "min")' in _ALERTS      # NULL -> '—'
    assert 'humanize_duration(im.get("MTTM_MIN"), "min")' in _ALERTS
    assert "ACKED_N" in _ALERTS and "AUTO-declared" in _ALERTS


# ---------------------------------------------------------------------------
# The V154 caption is schema-gated
# ---------------------------------------------------------------------------

def test_v154_caption_is_schema_gated():
    assert "if _v154_applied() else \"\")" in _CR
    gated = _CR.split("_loop_txt = (", 1)[1].split("if _v154_applied()", 1)[0]
    assert "attach to it" in gated and "MITIGATED" in gated and "closing stays human" in gated
    # the claim appears nowhere else on the page
    assert _CR.count("attach to it") == 1


@pytest.mark.parametrize("res, expected", [
    (QueryResult(df=pd.DataFrame({"VERSION": [152, 153]})), False),
    (QueryResult(df=pd.DataFrame({"VERSION": [153, 154]})), True),
    (QueryResult(df=pd.DataFrame({"VERSION": ["154"]})), True),
    (QueryResult(df=pd.DataFrame(), ok=False, error="no table"), False),
    (QueryResult(df=pd.DataFrame({"OTHER": [154]})), False),
])
def test_v154_applied_reads_the_schema_version_set(monkeypatch, res, expected):
    seen = {}

    def _fake_run(sql, **kw):
        seen.update(kw, sql=sql)
        return res

    monkeypatch.setattr(cr, "run", _fake_run)
    assert cr._v154_applied() is expected
    assert seen["sql"] == mart_sql.schema_version() and seen["tier"] == "metadata"
