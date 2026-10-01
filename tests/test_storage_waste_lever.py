"""Next-Fifty #35 storage leg (v4.605): unread-table storage joins the de-duplicated Addressable $/mo.

Pure + SQL + source locks (both CI legs; the rendered twin is tests/test_storage_waste_lever_shaped.py, skipped on
the streamlit floor). Cost ▸ Optimization & Savings ▸ Storage & waste's read-evidence scan (insights_sql.
storage_reclaim) gets one LEVER per table (logic.storage_waste): Archive or drop (a stale, unread table's active
bytes) or Cut retention (the Time Travel a 1-day retention would release on a written but unread table). The
panel publishes a session handoff (savings_rollup.storage_handoff) that Idle & sizing and Proof ▸ Pipeline read
back (storage_lever) exactly like the unread-maintenance lever, and one table is one saving across unread
maintenance, clustering, storage and retention."""

from __future__ import annotations

import ast
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.data import insights_sql, mart_sql
from app.logic import savings_rollup
from app.logic.decision import pipeline_frame, scenario_projection
from app.logic.ledger_measure import OBJECT_FINDING_TYPES, TABLE_FINDING_TYPES
from app.logic.savings_rollup import SavingsOpportunity, rollup_savings
from app.logic.storage_waste import (
    FLOOR_LEGEND,
    H_STORAGE_BASIS,
    LEVER_COLUMNS,
    LEVER_LEGEND,
    STORAGE_BOOKED_TYPES,
    TARGET_RETENTION_DAYS,
    lever_rows,
    reads_unavailable_note,
    storage_waste_verdicts,
    table_fqn,
)
from app.logic.unread_maintenance import booked_objects
from tests._source import ROOT, read

sqlglot = pytest.importorskip("sqlglot")

_RATE = 23.0                                          # $/TiB-month (the on-demand default)
_AS_OF = datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)  # the UI stamps aware UTC (formulas.utc_now)
_WHERES = ("Storage & waste", "Cost ▸ Optimization & Savings ▸ Storage & waste")


def _row(name: str, **kw) -> dict:
    """One storage_reclaim-shaped row (after the panel's DML_STATUS -> STATUS rename): by default a live, stale,
    never-read, unshared, 90+-day-old, never-cloned (the only live table in its clone group) table with nothing to
    free."""
    row = {"DATABASE_NAME": "DB", "SCHEMA_NAME": "S", "TABLE_NAME": name, "ACTIVE_GB": 0.0, "TIME_TRAVEL_GB": 0.0,
           "FAILSAFE_GB": 0.0, "CLONE_RETAINED_GB": 0.0, "RETENTION_DAYS": 1.0, "RETENTION_KNOWN": True,
           "LAST_DML": None, "LAST_READ": None, "STATUS": "STALE", "NEVER_READ": True, "SHARED_DATABASE": False,
           "OLDER_THAN_90D": True, "CLONE_GROUP_LIVE": 1}
    row.update(kw)
    return row


def _frame() -> pd.DataFrame:
    """One table per precedence step."""
    return pd.DataFrame([
        _row("GONE_T", RETENTION_KNOWN=False, ACTIVE_GB=1024.0, NEVER_READ=None),
        _row("UNK_T", NEVER_READ=None, ACTIVE_GB=1024.0),
        _row("READ_T", NEVER_READ=False, ACTIVE_GB=1024.0),
        _row("SHR_T", SHARED_DATABASE=True, ACTIVE_GB=1024.0),
        _row("YOUNG_T", OLDER_THAN_90D=False, ACTIVE_GB=1024.0),
        _row("STALE_T", ACTIVE_GB=1024.0),
        _row("WRITTEN_T", STATUS="ACTIVE", TIME_TRAVEL_GB=102.4, RETENTION_DAYS=5.0, ACTIVE_GB=500.0),
        _row("R1_T", STATUS="ACTIVE", TIME_TRAVEL_GB=50.0, RETENTION_DAYS=1.0),
        _row("EMPTY_T", ACTIVE_GB=0.0, TIME_TRAVEL_GB=10.0),
        _row("ODD_T", STATUS="WEIRD", ACTIVE_GB=100.0, TIME_TRAVEL_GB=10.0, RETENTION_DAYS=5.0),
    ])


_LEVERS = ["Object gone", "Unconfirmed", "Keep", "Check share consumers", "Newer than 90 days", "Archive or drop",
           "Cut retention", "Nothing to reclaim", "Nothing to reclaim", "Nothing to reclaim"]


def _ests(out: pd.DataFrame) -> list:
    return [None if math.isnan(v) else v for v in out["EST_MONTHLY_USD"]]


# --- 1-5: the verdicts (pure) ---------------------------------------------------------------------------------

def test_verdict_precedence_and_pricing():
    frame = _frame()
    out = storage_waste_verdicts(frame, rate_tb=_RATE)
    assert list(out["LEVER"]) == _LEVERS
    # $23.00 = 1 TiB active at $23/TiB-month; $1.84 = 102.4 GB TT x (5 - 1) / 5 / 1024 x $23
    assert _ests(out) == [None, None, None, None, None, 23.0, 1.84, None, None, None]
    assert list(out["TABLE_NAME"]) == list(frame["TABLE_NAME"])                  # same rows, same order
    assert list(out.index) == list(frame.index)
    assert list(out.columns) == [*list(frame.columns[:3]), *LEVER_COLUMNS, *list(frame.columns[3:])]
    assert list(frame.columns) == list(_frame().columns)                         # the input is not mutated
    # unknown SHARED_DATABASE counts as shared (a consumer's reads would be invisible)
    unknown = storage_waste_verdicts(pd.DataFrame([_row("T", SHARED_DATABASE=None, ACTIVE_GB=10.0)]), rate_tb=_RATE)
    assert list(unknown["LEVER"]) == ["Check share consumers"]
    # the target retention is a parameter (owner default 1 day, Snowflake's default)
    assert TARGET_RETENTION_DAYS == 1
    three = storage_waste_verdicts(frame, rate_tb=_RATE, target_days=3)
    assert three.loc[6, "LEVER"] == "Cut retention" and three.loc[6, "EST_MONTHLY_USD"] == 0.92   # x (5-3)/5
    # a $0 rate keeps the lever (eligibility does not depend on the rate) but nothing counts
    zero = storage_waste_verdicts(frame, rate_tb=0.0)
    assert list(zero["LEVER"]) == _LEVERS
    assert zero.loc[5, "EST_MONTHLY_USD"] == 0.0 and zero.loc[6, "EST_MONTHLY_USD"] == 0.0
    assert lever_rows(zero).empty
    assert list(lever_rows(out)["TABLE_NAME"]) == ["STALE_T", "WRITTEN_T"]
    # re-running on its own output replaces the two columns instead of duplicating them
    again = storage_waste_verdicts(out, rate_tb=_RATE)
    assert list(again.columns) == list(out.columns) and list(again["LEVER"]) == _LEVERS
    # the panel's pre-rename spelling (DML_STATUS) reads the same
    raw = frame.rename(columns={"STATUS": "DML_STATUS"})
    assert list(storage_waste_verdicts(raw, rate_tb=_RATE)["LEVER"]) == _LEVERS


def test_fail_safe_clone_and_stale_time_travel_never_count():
    base = storage_waste_verdicts(_frame(), rate_tb=_RATE)
    heavy = _frame().assign(FAILSAFE_GB=9999.0)
    heavy.loc[heavy["TABLE_NAME"] == "STALE_T", "TIME_TRAVEL_GB"] = 7777.0      # a stale table's TT ages out itself
    out = storage_waste_verdicts(heavy, rate_tb=_RATE)
    assert list(out["LEVER"]) == _LEVERS and _ests(out) == _ests(base)
    # clone-retained bytes are never added to an estimate; on a stale table they make it 'Check clones' (unpriced,
    # v4.605 review r1: a drop would free nothing a clone still references), and a written table's retention cut
    # is unchanged by them (Time Travel on a written table is still released)
    cloned = storage_waste_verdicts(_frame().assign(CLONE_RETAINED_GB=8888.0), rate_tb=_RATE)
    assert list(cloned["LEVER"]) == [*_LEVERS[:5], "Check clones", *_LEVERS[6:]]
    assert _ests(cloned) == [None, None, None, None, None, None, 1.84, None, None, None]
    fs_only = storage_waste_verdicts(pd.DataFrame([_row("FS_T", STATUS="ACTIVE", RETENTION_DAYS=30.0,
                                                        TIME_TRAVEL_GB=0.0, FAILSAFE_GB=500.0)]), rate_tb=_RATE)
    assert list(fs_only["LEVER"]) == ["Nothing to reclaim"] and math.isnan(fs_only.loc[0, "EST_MONTHLY_USD"])
    # a stale table is priced on its ACTIVE bytes only, however much Time Travel it still carries
    stale = storage_waste_verdicts(pd.DataFrame([_row("S_T", ACTIVE_GB=2048.0, TIME_TRAVEL_GB=4096.0,
                                                      RETENTION_DAYS=90.0)]), rate_tb=_RATE)
    assert list(stale["LEVER"]) == ["Archive or drop"] and stale.loc[0, "EST_MONTHLY_USD"] == 46.0


def test_a_stale_table_sharing_storage_with_a_clone_is_never_priced():
    """v4.605 review r1: a source owns the micro-partitions its clones share, so dropping it frees nothing a clone
    still references (they become retained-for-clone bytes of the dropped table). A stale, unread table in a clone
    group with another live table, or one retaining bytes for a clone, is 'Check clones' with no estimate; an
    unknown group count is unpriced too (unknown counts as shared, like SHARED_DATABASE)."""
    def lever(**kw):
        kw.setdefault("ACTIVE_GB", 2048.0)
        out = storage_waste_verdicts(pd.DataFrame([_row("T", **kw)]), rate_tb=_RATE)
        est = out.loc[0, "EST_MONTHLY_USD"]
        return out.loc[0, "LEVER"], None if math.isnan(est) else est

    assert lever() == ("Archive or drop", 46.0)                                 # never cloned: priced
    assert lever(CLONE_RETAINED_GB=4096.0) == ("Check clones", None)            # the reviewer's scratch case
    assert lever(CLONE_GROUP_LIVE=2) == ("Check clones", None)                  # an untouched clone source: 0 retained
    assert lever(CLONE_GROUP_LIVE="3") == ("Check clones", None)                # the driver's text spelling
    for unknown in (None, float("nan"), pd.NA, "CLONE_GROUP_LIVE_0"):
        assert lever(CLONE_GROUP_LIVE=unknown) == ("Check clones", None), unknown
    assert lever(CLONE_GROUP_LIVE=1.0, CLONE_RETAINED_GB=0.0) == ("Archive or drop", 46.0)
    # the earlier rules still win (first match): read, shared-out, too new
    assert lever(CLONE_GROUP_LIVE=2, NEVER_READ=False)[0] == "Keep"
    assert lever(CLONE_GROUP_LIVE=2, SHARED_DATABASE=True)[0] == "Check share consumers"
    assert lever(CLONE_GROUP_LIVE=2, OLDER_THAN_90D=False)[0] == "Newer than 90 days"
    # a written table's retention cut does not depend on clones (its Time Travel is still released)
    assert lever(STATUS="ACTIVE", CLONE_GROUP_LIVE=5, CLONE_RETAINED_GB=10.0, TIME_TRAVEL_GB=102.4,
                 RETENTION_DAYS=5.0, ACTIVE_GB=500.0) == ("Cut retention", 1.84)
    # never counted: not a lever row, never an opportunity
    frame = pd.DataFrame([_row("C_T", ACTIVE_GB=1024.0, CLONE_GROUP_LIVE=2), _row("P_T", ACTIVE_GB=1024.0)])
    out = storage_waste_verdicts(frame, rate_tb=_RATE)
    assert list(lever_rows(out)["TABLE_NAME"]) == ["P_T"]
    assert [o.target for o in savings_rollup.storage_waste_opportunities(out)] == ["DB.S.P_T"]
    # a frame without the group count (a builder that dropped it) confirms nothing
    assert set(storage_waste_verdicts(frame.drop(columns="CLONE_GROUP_LIVE"), rate_tb=_RATE)["LEVER"]) == {
        "Unconfirmed"}


def test_without_read_evidence_every_row_is_unconfirmed():
    for builder in (insights_sql.storage_waste("ALL"), mart_sql.table_storage_waste_mart("ALL")):
        cols = sqlglot.parse_one(builder, read="snowflake").named_selects
        assert "NEVER_READ" not in cols
        frame = pd.DataFrame([dict.fromkeys(cols, 1024.0), dict.fromkeys(cols, 5.0)]).assign(
            DATABASE_NAME="DB", SCHEMA_NAME="S", TABLE_NAME=["A", "B"])
        out = storage_waste_verdicts(frame, rate_tb=_RATE)
        assert list(out["LEVER"]) == ["Unconfirmed", "Unconfirmed"]
        assert out["EST_MONTHLY_USD"].isna().all() and lever_rows(out).empty
    # one missing evidence column is enough
    for col in ("OLDER_THAN_90D", "CLONE_GROUP_LIVE"):
        assert set(storage_waste_verdicts(_frame().drop(columns=col), rate_tb=_RATE)["LEVER"]) == {"Unconfirmed"}
    for empty in (None, pd.DataFrame(), pd.DataFrame(columns=["TABLE_NAME"])):
        out = storage_waste_verdicts(empty, rate_tb=_RATE)
        assert out.empty and set(LEVER_COLUMNS) <= set(out.columns)
    assert lever_rows(None).empty and lever_rows(pd.DataFrame({"LEVER": ["Archive or drop"]})).empty
    # the shaped harness types SHARED_DATABASE / STATUS as strings and the flags as numbers: never a raise
    shaped = pd.DataFrame([{**_row("X"), "SHARED_DATABASE": "SHARED_DATABASE_0", "STATUS": "DML_STATUS_0",
                            "NEVER_READ": 1.0, "OLDER_THAN_90D": 2.0, "RETENTION_KNOWN": 1.0,
                            "RETENTION_DAYS": "RETENTION_DAYS_0", "ACTIVE_GB": "x"},
                           {**_row("Y"), "NEVER_READ": pd.NA, "SHARED_DATABASE": pd.NA, "RETENTION_KNOWN": pd.NA}])
    out = storage_waste_verdicts(shaped, rate_tb=_RATE)
    assert list(out["LEVER"]) == ["Nothing to reclaim", "Object gone"]
    assert storage_waste_verdicts(pd.DataFrame([_row("N", NEVER_READ=float("nan"))]), rate_tb=_RATE)[
        "LEVER"].tolist() == ["Unconfirmed"]


def _as_run_stores(raw: str) -> tuple[str, str]:
    """(error_kind, error) exactly as run() fills them from a raw Snowflake error: the kind from the RAW text, the
    error through format_snowflake_error (which drops the object name from a not-visible error)."""
    from app.core.errors import format_snowflake_error
    from app.core.query import _classify_error
    exc = Exception(raw)
    return _classify_error(exc), format_snowflake_error(exc)


def test_reads_unavailable_note_by_kind():
    ah = ("ACCESS_HISTORY is not visible to this app (it needs Enterprise edition and IMPORTED PRIVILEGES on the "
          "SNOWFLAKE database)")
    not_visible = ("ACCESS_HISTORY or GRANTS_TO_ROLES (the share guard), or another ACCOUNT_USAGE view this scan "
                   "reads, is not visible to this app (they need IMPORTED PRIVILEGES on the SNOWFLAKE database; "
                   "ACCESS_HISTORY also needs Enterprise edition)")
    # v4.605 review r2 (R2-3 / R2-4 / R2-10): run() stores format_snowflake_error's text, which names no object, so a
    # not-visible failure of any object the scan reads gives the one note that names both and the SNOWFLAKE grant.
    # The inputs go through the same path run() takes (a raw-text input never reaches the note in the app).
    setup = "The current role cannot access this object. If OVERWATCH setup is new, run the migrations and roles.sql."
    for obj in ("ACCESS_HISTORY", "GRANTS_TO_ROLES", "TABLE_DML_HISTORY", "TABLES"):
        kind, err = _as_run_stores(f"002003 (02000): SQL compilation error:\nObject 'SNOWFLAKE.ACCOUNT_USAGE.{obj}' "
                                   "does not exist or not authorized.")
        assert (kind, err) == ("absent", setup), obj
        assert reads_unavailable_note(kind, err) == not_visible, obj
    # the note never depends on the error text for a not-visible kind (the old per-object branch never ran)
    assert reads_unavailable_note("absent") == reads_unavailable_note(" ABSENT ", "anything") == not_visible
    assert reads_unavailable_note("absent", "Object 'SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES' does not exist or not "
                                            "authorized.") == not_visible
    # "Insufficient privileges" is its own kind (R2-1): the literal-'absent' branch does not take it -- a failed read
    # that shows the error itself
    kind, err = _as_run_stores("003001 (42501): SQL access control error: Insufficient privileges to operate on "
                               "view 'ACCESS_HISTORY'")
    # the error's own full stop is dropped: the degraded caption appends one (review r3: it read "roles.sql..")
    assert kind == "privilege" and reads_unavailable_note(kind, err) == (
        "the access-history read failed: " + setup.rstrip("."))
    assert not reads_unavailable_note(kind, err).endswith(".")
    # the edition text survives format_snowflake_error, so the edition branch still fires on the real path
    kind, err = _as_run_stores("Unsupported feature 'ACCESS_HISTORY'")
    assert reads_unavailable_note(kind, err) == ah
    kind, err = _as_run_stores("000604 (57014): Statement reached its statement or warehouse timeout of 180 second(s).")
    assert kind == "timeout" and reads_unavailable_note(kind, err) == "the 90-day access-history read timed out"
    assert reads_unavailable_note("sql", "Unsupported feature 'ACCESS_HISTORY'") == ah
    assert reads_unavailable_note("", "This view requires ENTERPRISE edition") == ah
    assert reads_unavailable_note("timeout", "Statement reached its statement or warehouse timeout of 180 "
                                             "second(s).") == "the 90-day access-history read timed out"
    assert reads_unavailable_note(" TIMEOUT ") == "the 90-day access-history read timed out"
    assert reads_unavailable_note("sql", "  SQL compilation error:\n  invalid identifier  'X' ") == (
        "the access-history read failed: SQL compilation error: invalid identifier 'X'")
    assert reads_unavailable_note("sql") == "the access-history read failed"
    assert reads_unavailable_note(None, None) == "the access-history read failed"
    long = reads_unavailable_note("sql", "e" * 1000)
    assert long == "the access-history read failed: " + "e" * 300
    for kind, err in (("absent", ""), ("absent", setup), ("privilege", setup), ("timeout", ""), ("sql", "boom"),
                      ("", "")):
        assert "Database filter" not in reads_unavailable_note(kind, err)       # it never narrows this scan


def test_storage_booked_types_are_retention_plus_unread():
    assert STORAGE_BOOKED_TYPES == TABLE_FINDING_TYPES | OBJECT_FINDING_TYPES
    assert sorted(STORAGE_BOOKED_TYPES) == ["DROP_SEARCH_OPTIMIZATION", "RETENTION", "SUSPEND_MV_REFRESH",
                                            "SUSPEND_RECLUSTER"]
    ledger = pd.DataFrame([
        {"TARGET_OBJECT": "DB.S.R1", "FINDING_TYPE": "RETENTION", "STATE": "ESTIMATED"},
        {"TARGET_OBJECT": '"db"."s"."r2"', "FINDING_TYPE": " retention ", "STATE": " verified "},
        {"TARGET_OBJECT": "db.s.r3", "FINDING_TYPE": "RETENTION", "STATE": "VERIFIED"},
        {"TARGET_OBJECT": "DB.S.REJ", "FINDING_TYPE": "RETENTION", "STATE": "REJECTED"},
        {"TARGET_OBJECT": "DB.S.NUL", "FINDING_TYPE": "RETENTION", "STATE": None},
        {"TARGET_OBJECT": "DB.S.CL", "FINDING_TYPE": "SUSPEND_RECLUSTER", "STATE": "ESTIMATED"},
        {"TARGET_OBJECT": "WH_A", "FINDING_TYPE": "AUTO_SUSPEND", "STATE": "ESTIMATED"},
    ])
    assert booked_objects(ledger, finding_types=STORAGE_BOOKED_TYPES) == frozenset(
        {"DB.S.R1", "DB.S.R2", "DB.S.R3", "DB.S.CL"})
    # the default call (the unread-maintenance lever) is exactly the pre-change set
    assert booked_objects(ledger) == frozenset({"DB.S.CL"})
    # a lower-case / padded type list is normalized like the ledger cells
    assert booked_objects(ledger, finding_types=frozenset({" retention "})) == frozenset(
        {"DB.S.R1", "DB.S.R2", "DB.S.R3"})
    assert booked_objects(None, finding_types=STORAGE_BOOKED_TYPES) is None
    assert booked_objects(pd.DataFrame(), finding_types=STORAGE_BOOKED_TYPES) == frozenset()
    assert booked_objects(ledger.drop(columns="STATE"), finding_types=STORAGE_BOOKED_TYPES) is None


# --- 6-13: the handoff, the lever and the rollup (pure) --------------------------------------------------------

def _verdicts(*names: str) -> pd.DataFrame:
    out = storage_waste_verdicts(_frame(), rate_tb=_RATE)
    return out[out["TABLE_NAME"].isin(names)].reset_index(drop=True) if names else out


def _shand(verdicts=None, status="confirmed", **kw) -> dict:
    kw.setdefault("company", "ALFA")
    kw.setdefault("database", "")
    kw.setdefault("scope", "S")
    kw.setdefault("as_of", _AS_OF)
    kw.setdefault("rate", _RATE)
    return savings_rollup.storage_handoff(verdicts, status=status, **kw)


def _slever(handoff, *, company="ALFA", scope="S", age_sec=60.0, where=_WHERES[0], rate=_RATE):
    return savings_rollup.storage_lever(handoff, company=company, scope=scope,
                                        now=_AS_OF + timedelta(seconds=age_sec), rate=rate, where=where)


def test_storage_opportunities_round_trip_and_single_call_site():
    verdicts = _verdicts()
    opps = savings_rollup.storage_waste_opportunities(verdicts)
    assert [(o.source, o.target, o.monthly_usd, o.confidence) for o in opps] == [
        ("STORAGE", "DB.S.STALE_T", 23.0, 0.6), ("RETENTION", "DB.S.WRITTEN_T", 1.84, 0.6)]
    assert savings_rollup.storage_waste_opportunities(None) == []
    assert savings_rollup.storage_waste_opportunities(_frame()) == []           # no LEVER column: nothing
    h = _shand(verdicts)
    assert h["rows"] == [["STORAGE", "DB.S.STALE_T", 23.0, 0.6], ["RETENTION", "DB.S.WRITTEN_T", 1.84, 0.6]]
    lever = _slever(h)
    assert lever.included and lever.reason == "" and lever.note == ""
    assert lever.opportunities == tuple(opps)
    assert table_fqn({"DATABASE_NAME": " DB ", "SCHEMA_NAME": "S", "TABLE_NAME": None}) == "DB.S."
    # the headlines never build the lever themselves: the ONLY call site outside tests is storage_handoff's body
    for rel in ("app/ui/pages/cost_parts/optimize.py", "app/ui/decision_studio.py"):
        assert "storage_waste_opportunities(" not in read(rel), rel
    calls = [(str(py.relative_to(ROOT)), src.count("storage_waste_opportunities("))
             for py in sorted((ROOT / "app").rglob("*.py"))
             if "storage_waste_opportunities(" in (src := py.read_text(encoding="utf-8")
                                                   .replace("def storage_waste_opportunities(", ""))]
    assert calls == [(str(Path("app/logic/savings_rollup.py")), 1)], calls
    body = read("app/logic/savings_rollup.py").split("def storage_handoff(", 1)[1].split("\ndef ", 1)[0]
    assert body.count("storage_waste_opportunities(verdicts)") == 1


def test_storage_handoff_statuses_booked_and_primitives():
    verdicts = _verdicts()
    for status in ("clean", "no_reads", "scan_failed", "ledger_failed", "bogus"):
        assert _shand(verdicts, status=status)["rows"] == [], status          # only CONFIRMED carries rows
    failed = _shand(verdicts, booked=None)
    assert failed["status"] == "ledger_failed" and failed["rows"] == [] and failed["booked_excluded"] == 0
    assert _shand(_verdicts("READ_T"), booked=None)["status"] == "confirmed"   # nothing to count: nothing to leave out
    assert _shand(None, booked=None)["status"] == "confirmed"
    quoted = booked_objects(pd.DataFrame([{"TARGET_OBJECT": '"db"."s"."stale_t"', "FINDING_TYPE": "RETENTION",
                                           "STATE": "ESTIMATED"}]), finding_types=STORAGE_BOOKED_TYPES)
    booked = _shand(verdicts, booked=quoted)
    assert booked["rows"] == [["RETENTION", "DB.S.WRITTEN_T", 1.84, 0.6]] and booked["booked_excluded"] == 1
    for status in ("confirmed", "clean", "no_reads", "scan_failed"):
        h = _shand(verdicts, status=status, checked=50, truncated=True, booked=frozenset({"X"}))
        assert json.loads(json.dumps(h)) == h, status                           # primitives only
        assert all(isinstance(v, (str, int, float, bool, list)) for v in h.values())
    h = _shand(verdicts, company="  alfa ", database=" db1 ", rate=23)
    assert h["company"] == "ALFA" and h["database"] == "DB1" and h["scope"] == "S"
    assert h["as_of"] == "2026-09-30T10:00:00+00:00" and isinstance(h["rate"], float) and h["rate"] == 23.0
    assert _shand(None, company="")["company"] == "ALL"
    assert savings_rollup.STORAGE_HANDOFF_KEY == "_ow_storage_waste_handoff"
    assert savings_rollup.STORAGE_HANDOFF_KEY != savings_rollup.UNREAD_HANDOFF_KEY
    assert not savings_rollup.STORAGE_HANDOFF_KEY.startswith(("flt_", "cost_", "opt_"))   # never a widget key


_RS = {
    "not_run": "not checked this session: run the storage-waste scan in {where}",
    "company": "last checked for ALFA, not TRXS: re-run the scan in {where}",
    "database": "last checked for database DB1 only: clear the Database filter and re-run the scan in {where}",
    "stale": "the last check was shown over 1h ago, or cached data was refreshed since: re-run the scan in {where}",
    "scan": "the storage-waste scan could not be read in {where}",
    "no_reads": "the storage-waste scan had no read evidence, so no table is confirmed unread",
    "ledger": "the Savings ledger could not be read, so objects already booked there could not be left out",
    "rate": ("the storage rate changed since the last check, which priced its tables at the old rate: re-run the "
             "scan in {where}"),
}


@pytest.mark.parametrize(("handoff", "kwargs", "reason"), [
    (None, {}, "not_run"),
    ("not a mapping", {}, "not_run"),
    ({"status": "bogus"}, {}, "not_run"),
    ({"status": ["confirmed"]}, {}, "not_run"),
    ({"status": "confirm_failed"}, {}, "not_run"),               # an unread-maintenance status is not ours
    ("confirmed", {"company": "trxs"}, "company"),
    ({"database": "db1"}, {}, "database"),
    ("confirmed", {"scope": "S2"}, "stale"),
    ("confirmed", {"age_sec": 3601}, "stale"),
    ({"as_of": "not a timestamp"}, {}, "stale"),
    ({"as_of": "2026-09-30T10:00:00"}, {}, "stale"),             # a naive wall-clock stamp is never fresh
    ("confirmed", {"age_sec": -61}, "stale"),
    ("confirmed", {"age_sec": -3600}, "stale"),
    ("confirmed", {"rate": 25.0}, "rate"),                       # the rows were priced at $23/TiB
    ({"rate": None}, {}, "rate"),
    ({"rate": "23"}, {}, "rate"),
    ({"status": "scan_failed"}, {}, "scan"),
    ({"status": "no_reads"}, {}, "no_reads"),
    ({"status": "ledger_failed"}, {}, "ledger"),
])
def test_storage_lever_absent_reasons(handoff, kwargs, reason):
    base = _shand(_verdicts())
    h = {**base, **handoff} if isinstance(handoff, dict) else (base if handoff == "confirmed" else handoff)
    for where in _WHERES:
        lever = _slever(h, where=where, **kwargs)
        assert lever.included is False and lever.opportunities == () and lever.note == ""
        assert lever.reason == _RS[reason].format(where=where)


def test_storage_lever_included_states_notes_and_bounds():
    clean = _slever(_shand(None, status="clean"))
    assert clean.included and clean.opportunities == () and clean.reason == "" and clean.note == ""
    one = _verdicts("STALE_T")
    booked = _slever(_shand(one, booked=frozenset({"DB.S.STALE_T"}), checked=50, truncated=True))
    assert booked.included and booked.opportunities == ()                        # counted, at $0
    assert booked.note == ("only the top 50 tables by retention bytes were checked, so this is a floor; 1 already "
                           "booked on the Savings ledger left out")
    floor = _slever(_shand(_verdicts(), checked=1234, truncated=True))
    assert floor.note == "only the top 1,234 tables by retention bytes were checked, so this is a floor"
    assert [(o.source, o.target, o.monthly_usd) for o in floor.opportunities] == [
        ("STORAGE", "DB.S.STALE_T", 23.0), ("RETENTION", "DB.S.WRITTEN_T", 1.84)]
    # malformed rows are skipped, never raised on
    odd = _slever({**_shand(_verdicts()), "rows": [
        ["STORAGE", "X", 5.0], "bad", None, ["IDLE", "WH_A", 5.0, 0.6], ["STORAGE", "Y", -1, 0.6],
        ["RETENTION", "Z", 0, 0.6], ["STORAGE", "", 3, 0.6], ["STORAGE", None, 2, 0.6], [["STORAGE"], "L", 4, 0.6],
        ["RETENTION", "OK", "7.5", "0.6"]]})
    assert odd.included and [(o.source, o.target, o.monthly_usd, o.confidence) for o in odd.opportunities] == [
        ("RETENTION", "OK", 7.5, 0.6)]
    assert _slever({**_shand(_verdicts()), "rows": "nope"}).opportunities == ()
    # the freshness bounds are the unread lever's
    h = _shand(_verdicts())
    assert _slever(h, age_sec=3600).included and not _slever(h, age_sec=3601).included
    assert _slever(h, age_sec=-60).included and not _slever(h, age_sec=-61).included
    assert _slever(h, rate=_RATE + 1e-12).included                               # float noise is the same rate
    # a $0 scan (clean, or every qualifying table booked) is the same at any rate
    assert _slever(_shand(None, status="clean"), rate=99.0).included
    assert _slever(_shand(one, booked=frozenset({"DB.S.STALE_T"})), rate=99.0).included
    assert not _slever(_shand(_verdicts(), company="ALL"), company="ALFA").included


def test_storage_handoff_note_wording():
    note = savings_rollup.storage_handoff_note
    for silent in (None, "x", {"status": "bogus"}, _shand(None, status="clean"),
                   _shand(_verdicts(), status="scan_failed"), _shand(_verdicts(), booked=None)):
        assert note(silent) == "", silent
    assert note(_shand(None, status="no_reads")) == (
        "Not added to Addressable $/mo: without read evidence no table is confirmed unread.")
    assert note(_shand(None)) == "No unread table to add to Addressable $/mo."
    assert note(_shand(_verdicts(), booked=frozenset({"DB.S.STALE_T", "DB.S.WRITTEN_T"}))) == (
        "No unread table to add to Addressable $/mo: the 2 that qualify are already booked on the Savings ledger.")
    elsewhere = ("An object booked in another session keeps counting here until the scan is re-run at least 5m "
                 "after that booking (the Savings-ledger read that leaves booked objects out is cached for up to "
                 "5m): at most 1h 5m after the booking.")
    assert note(_shand(_verdicts())) == (
        "2 unread table(s) join Addressable $/mo in Idle & sizing and on Proof ▸ Pipeline for this Company. They "
        "drop out when cached data is refreshed, the storage rate changes, or 1h after this panel was last shown. "
        + elsewhere)
    assert note(_shand(_verdicts(), checked=50, truncated=True, booked=frozenset({"DB.S.WRITTEN_T"}))) == (
        "1 unread table(s) join Addressable $/mo in Idle & sizing and on Proof ▸ Pipeline for this Company (a floor: "
        "only the top 50 tables by retention bytes were checked). 1 already booked on the Savings ledger are left "
        "out. They drop out when cached data is refreshed, the storage rate changes, or 1h after this panel was "
        "last shown. " + elsewhere)
    assert savings_rollup.S_STORAGE_LEDGER_UNAVAILABLE == (
        "The Savings ledger could not be read, so unread tables are not added to Addressable $/mo (tables already "
        "booked could not be left out).")
    assert LEVER_LEGEND == (
        "LEVER says whether a table qualifies for Addressable $/mo (Idle & sizing, Proof ▸ Pipeline); tables already "
        "booked on the Savings ledger are then left out (the line at the end of this panel counts them). Archive or "
        "drop: no DML and no read in 90 days, and no clone shares its storage; priced on its active bytes, which a "
        "drop frees once they age out of Time Travel and fail-safe. Cut retention: still written but not read in 90 "
        "days; priced on the Time Travel a 1-day retention would release (Time Travel × (retention − 1) ÷ "
        "retention, the retention control's own estimate). That is a monthly run-rate only while the table keeps "
        "being written as it was over its retention window (a one-off rewrite ages out of Time Travel on its own), "
        "and it assumes no account-level MIN_DATA_RETENTION_TIME_IN_DAYS above 1 day (not checked here): a higher "
        "floor keeps that Time Travel whatever the table's own setting. Keep: read in 90 days. Check share "
        "consumers: its database is shared out, and a consumer account's reads never reach this account's access "
        "history. Newer than 90 days: too new for a 90-day no-read claim. Check clones: a stale, unread table that "
        "shares storage with a clone (another live table in its clone group, or bytes it retains for a clone); not "
        "priced, because dropping it frees nothing a clone still references. Object gone: no live table under that "
        "ID (dropped or replaced). Nothing to reclaim: not a lever. A written table is not a drop candidate, and it "
        "is already at 1 day or less of retention or has no Time Travel; a stale table has no active bytes for a "
        "drop to free, and its Time Travel ages out on its own. Fail-safe (a fixed 7-day tail) and clone-retained "
        "bytes (a clone still holds them) never count. Confirm with the owner before dropping anything: reads from a replica in another "
        "account, and reads rarer than every 90 days, are invisible here.")
    # review r2 R2-5 / R2-11: both Addressable $/mo help texts carry H_STORAGE_BASIS, so the floor assumption is here
    assert H_STORAGE_BASIS == (
        "a stale table's active bytes, or the Time Travel a 1-day retention would release on a table still written "
        "(a monthly run-rate only while it keeps being written, assuming no account-level "
        "MIN_DATA_RETENTION_TIME_IN_DAYS above 1 day), at your storage rate")
    # review r2 R2-6: a written table showing active bytes lands in Nothing to reclaim, so the legend never says a
    # drop would free nothing there (it says a written table is not a drop candidate)
    assert "nothing a drop" not in LEVER_LEGEND
    written = storage_waste_verdicts(pd.DataFrame([_row("W", STATUS="ACTIVE", ACTIVE_GB=2048.0, TIME_TRAVEL_GB=512.0,
                                                        RETENTION_DAYS=1)]), rate_tb=_RATE)
    assert written["LEVER"].tolist() == ["Nothing to reclaim"]
    assert FLOOR_LEGEND == (
        "The scan ranks tables by retention bytes, and a table with no DML for 90 days has little or no Time Travel "
        "or fail-safe left, so most stale tables fall outside the top 50: Archive or drop is a floor.")
    for text in (LEVER_LEGEND, FLOOR_LEGEND, H_STORAGE_BASIS, savings_rollup.N_S_FLOOR, savings_rollup.R_S_RATE):
        assert not re.search(r"\d+(?:\.\d+)?s\b", text), text                  # no raw seconds anywhere


def test_scope_reason_extraction_keeps_unread_byte_identical():
    from app.logic.unread_maintenance import unread_maintenance_verdicts
    short = pd.DataFrame([{"OBJECT_FQN": "D.S.A", "CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 4.0}])
    reads = pd.DataFrame([{"OBJECT_FQN": "D.S.A", "MATCHED_BY_ID": True, "SHARED_DATABASE": False,
                           "READ_QUERIES": 0, "READ_USERS": 0, "LAST_READ": None, "WRITE_QUERIES": 0}])
    h = savings_rollup.unread_handoff(unread_maintenance_verdicts(short, reads, rate=2.0), status="confirmed",
                                      company="ALFA", database="", scope="S", as_of=_AS_OF, rate=2.0)

    def unread(handoff, **kw):
        now = kw.pop("now", _AS_OF + timedelta(seconds=60))
        kw.setdefault("company", "ALFA")
        kw.setdefault("scope", "S")
        return savings_rollup.unread_lever(handoff, now=now, rate=2.0, where="Storage & waste", **kw).reason

    assert unread(h) == ""
    assert unread(h, company="trxs") == "last checked for ALFA, not TRXS: re-run the scan in Storage & waste"
    assert unread({**h, "database": "db1"}) == (
        "last checked for database DB1 only: clear the Database filter and re-run the scan in Storage & waste")
    stale = ("the last check was shown over 1h ago, or cached data was refreshed since: re-run the scan in Storage "
             "& waste")
    assert unread(h, scope="S2") == stale and unread(h, now=_AS_OF + timedelta(seconds=3601)) == stale
    assert unread({**h, "as_of": "2026-09-30T10:00:00"}) == stale
    # the shared helper, directly: '' when in scope and fresh; the first failing check otherwise
    reason = savings_rollup._scope_reason
    assert reason(h, company="ALFA", scope="S", now=_AS_OF, where="W") == ""
    assert reason({**h, "database": "x"}, company="TRXS", scope="S2", now=_AS_OF, where="W").startswith(
        "last checked for ALFA, not TRXS")
    for fn in ("unread_lever", "storage_lever"):
        body = read("app/logic/savings_rollup.py").split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert body.count("_scope_reason(handoff, company=company, scope=scope, now=now, where=where)") == 1, fn


def test_lever_basis_and_short_name_storage_waste():
    assert savings_rollup.lever_basis(["IDLE", "STORAGE_WASTE"], {"UNREAD_MAINT": "x"},
                                      {"STORAGE_WASTE": savings_rollup.N_S_FLOOR.format(checked=50)}) == (
        "Levers counted: idle timer + storage waste (only the top 50 tables by retention bytes were checked, so this "
        "is a floor). Not counted: unread maintenance (x).")
    assert savings_rollup.lever_short(["IDLE", "UNREAD_MAINT", "STORAGE_WASTE"]) == (
        "idle timer + unread maintenance + storage waste")
    assert savings_rollup.lever_basis([], {"STORAGE_WASTE": savings_rollup.R_S_NO_READS}) == (
        "Levers counted: none. Not counted: storage waste (the storage-waste scan had no read evidence, so no table "
        "is confirmed unread).")


def _opp(source: str, target: str, usd: float, conf: float = 0.6) -> SavingsOpportunity:
    return SavingsOpportunity(source, target, usd, conf)


def test_one_saving_per_table_across_object_levers():
    roll = rollup_savings([_opp("STORAGE", "DB.S.T", 23.0), _opp("UNREAD_MAINT", "DB.S.T", 36.8)])
    assert [(o.source, o.monthly_usd) for o in roll.items] == [("UNREAD_MAINT", 36.8)]
    assert [o.source for o in roll.dropped] == ["STORAGE"] and roll.total_monthly_usd == 36.8
    roll = rollup_savings([_opp("RETENTION", "DB.S.T", 12.0), _opp("CLUSTERING", "DB.S.T", 4.0)])
    assert [o.source for o in roll.items] == ["RETENTION"] and [o.source for o in roll.dropped] == ["CLUSTERING"]
    roll = rollup_savings([_opp("RETENTION", "DB.S.T", 2.0), _opp("STORAGE", "DB.S.T", 3.0)])
    assert [o.source for o in roll.items] == ["STORAGE"] and roll.total_monthly_usd == 3.0
    # a quoted / lower-case twin of the same table merges; the kept item keeps its own raw target
    roll = rollup_savings([_opp("UNREAD_MAINT", 'DB."s".t', 36.8), _opp("STORAGE", "DB.S.T", 23.0)])
    assert [(o.source, o.target) for o in roll.items] == [("UNREAD_MAINT", 'DB."s".t')] and len(roll.dropped) == 1
    roll = rollup_savings([_opp("UNREAD_MAINT", 'DB."s".t', 10.0), _opp("STORAGE", "DB.S.T", 23.0)])
    assert [(o.source, o.target) for o in roll.items] == [("STORAGE", "DB.S.T")]
    # never across groups: a storage row on a name that is also a warehouse's
    roll = rollup_savings([_opp("STORAGE", "WH_A", 5.0), _opp("IDLE", "WH_A", 9.0)])
    assert len(roll.items) == 2 and not roll.dropped and roll.total_monthly_usd == 14.0
    # the warehouse group stays byte-identical: the raw name, so a lower-case twin does NOT merge
    roll = rollup_savings([_opp("IDLE", "wh_a", 9.0), _opp("RESIZE", "WH_A", 5.0)])
    assert len(roll.items) == 2 and not roll.dropped
    # the groups are disjoint and name every overlapping source once
    groups = savings_rollup._OVERLAP_GROUPS
    assert groups == (frozenset({"IDLE", "RESIZE"}), frozenset({"CLUSTERING", "UNREAD_MAINT", "STORAGE",
                                                                "RETENTION"}))
    assert not groups[0] & groups[1]
    assert savings_rollup.effort_tier("STORAGE") == savings_rollup.effort_tier("RETENTION") == "MEDIUM"


def test_pipeline_frame_titles_and_entities_for_storage():
    opps = [_opp("STORAGE", "DB.S.STALE_T", 23.0), _opp("RETENTION", "DB.S.WRITTEN_T", 1.84),
            _opp("IDLE", "WH_A", 10.0)]
    pf = pipeline_frame(opps, None).set_index("SOURCE_ENTITY_KEY")
    assert pf.loc["DB.S.STALE_T", "SOURCE_ENTITY_TYPE"] == "OBJECT"
    assert pf.loc["DB.S.STALE_T", "TITLE"] == "Archive or drop unread DB.S.STALE_T"
    assert pf.loc["DB.S.WRITTEN_T", "SOURCE_ENTITY_TYPE"] == "OBJECT"
    assert pf.loc["DB.S.WRITTEN_T", "TITLE"] == "Cut Time Travel retention on unread DB.S.WRITTEN_T"
    assert pf.loc["WH_A", "SOURCE_ENTITY_TYPE"] == "WAREHOUSE"
    queued = pd.DataFrame([{"ACTION_ID": "9", "STATUS": "OPEN", "CONFIDENCE": 0.9, "ESTIMATED_USD": 30.0,
                            "PERIOD": "MONTHLY", "SOURCE_ENTITY_TYPE": "OBJECT", "SOURCE_ENTITY_KEY": "db.s.stale_t",
                            "SEVERITY": "LOW", "TITLE": "q"}])
    same = scenario_projection(pipeline_frame(opps[:1], queued), adoption_pct=100, realization_pct=100,
                               confidence_floor=0.6)
    assert same["candidates"] == 1.0 and same["gross_estimate"] == 30.0          # one table, the larger wins


def test_storage_logic_stays_pure():
    src = read("app/logic/storage_waste.py")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert not [m for m in mods if m == "streamlit" or m.startswith(("streamlit.", "app.data", "app.ui"))], mods
    assert "account_now(" not in src and "datetime.now(" not in src and "date.today(" not in src
    assert "except Exception" not in src                                        # never raises by construction


# --- 16: the SQL ------------------------------------------------------------------------------------------------

def test_storage_reclaim_carries_the_lever_evidence():
    sql = insights_sql.storage_reclaim("ALFA")
    assert ("shared_db AS (\n    SELECT DISTINCT UPPER(NAME) AS DB\n    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES\n"
            "    WHERE GRANTED_TO = 'SHARE' AND GRANTED_ON = 'DATABASE' AND DELETED_ON IS NULL\n)") in sql
    # the #30 confirm's share predicate, verbatim
    confirm = insights_sql.object_reads_confirm(("DB.S.T",))
    assert "WHERE GRANTED_TO = 'SHARE' AND GRANTED_ON = 'DATABASE' AND DELETED_ON IS NULL" in confirm
    assert "(sd.DB IS NOT NULL)                        AS SHARED_DATABASE" in sql
    assert ("IFF(m.TABLE_CREATED <= DATEADD('day', -90, CURRENT_TIMESTAMP()), TRUE, FALSE) AS OLDER_THAN_90D"
            in sql)
    assert "LEFT JOIN shared_db sd ON sd.DB = UPPER(m.TABLE_CATALOG)" in sql
    assert sql.index("LEFT JOIN reads r ON r.TABLE_ID = m.ID") < sql.index("LEFT JOIN shared_db sd")
    # any-domain reads (the safe direction): the D4 objectId join stays
    assert "objectDomain" not in sql
    assert 'f.value:"objectId"::NUMBER AS TABLE_ID' in sql and 'f.value:"objectId" IS NOT NULL' in sql
    assert "LEFT JOIN reads r ON r.TABLE_ID = m.ID" in sql
    # one statement, the same shortlist
    assert sql.rstrip().endswith("LIMIT 50")
    assert ("ORDER BY (m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES + COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0)) DESC"
            in sql)
    tree = sqlglot.parse_one(sql, read="snowflake")
    assert tree.named_selects[-5:] == ["DML_STATUS", "NEVER_READ", "SHARED_DATABASE", "OLDER_THAN_90D",
                                       "CLONE_GROUP_LIVE"]
    assert len(sqlglot.parse(sql, read="snowflake")) == 1
    # v4.605 review r1: the live clone-group size, counted over EVERY live table in the account -- before, and
    # independent of, the Company scope and the LIMIT (a clone can sit in another Company's database, or outside
    # the top 50) -- and joined on the row's CLONE_GROUP_ID
    cte = ("clone_groups AS (\n    SELECT CLONE_GROUP_ID, COUNT(*) AS CLONE_GROUP_LIVE\n"
           "    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS\n"
           "    WHERE DELETED = FALSE AND CLONE_GROUP_ID IS NOT NULL\n    GROUP BY 1\n)")
    assert cte in sql and sql.index(cte) < sql.index("\nSELECT\n    m.TABLE_CATALOG AS DATABASE_NAME")
    assert "LEFT JOIN clone_groups cg ON cg.CLONE_GROUP_ID = m.CLONE_GROUP_ID" in sql
    assert "    cg.CLONE_GROUP_LIVE\nFROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS m" in sql
    head = sql.split(cte, 1)[0] + cte                                           # every CTE, up to the group count
    assert insights_sql.storage_reclaim("ALL").split(cte, 1)[0] + cte == head   # the Company never reaches them
    assert "ALFA" not in head and "ALFA" in sql.split(cte, 1)[1]
    groups = next(c for c in tree.find_all(sqlglot.exp.CTE) if c.alias == "clone_groups").this
    assert groups.args.get("limit") is None and "TABLE_CATALOG" not in groups.sql(dialect="snowflake")
    # the fallbacks are unchanged (V124 parity): neither new column
    for fallback in (insights_sql.storage_waste("ALFA"), mart_sql.table_storage_waste_mart("ALFA")):
        assert "SHARED_DATABASE" not in fallback and "OLDER_THAN_90D" not in fallback


# --- 17-19: the wiring (source; the floor leg skips the shaped twin) -----------------------------------------

def _joined(src: str) -> str:
    return re.sub(r'"\s*\n\s*f?"', "", src)


def _waste_block() -> str:
    opt = read("app/ui/pages/cost_parts/optimize.py")
    return opt.split('"**Storage waste (Time Travel / failsafe / stale tables)**"', 1)[1].split(
        '"**Automatic clustering spend (per table)**"', 1)[0]


def test_storage_waste_panel_publishes_the_handoff_source():
    opt = read("app/ui/pages/cost_parts/optimize.py")
    block = _waste_block()
    write = "st.session_state[STORAGE_HANDOFF_KEY] = storage_handoff("
    assert block.count(write) == 3 and opt.count(write) == 3
    toggle = block.index('key="cost_waste_toggle"')
    writes = [m.start() for m in re.finditer(re.escape(write), block)]
    assert all(toggle < w for w in writes)
    for w in writes:
        call = block[w:w + 400]
        assert "as_of=utc_now(), rate=_waste_rate_tb" in call and 'database=""' in call
    assert block.count("as_of=utc_now(), rate=_waste_rate_tb") == 3
    assert "rate=rate" not in block                                             # never the credit rate
    assert ('_waste_rate_tb = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), DEFAULT_STORAGE_USD_PER_TB_MONTH)'
            in block)
    assert block.index("_waste_rate_tb = ") < toggle + 400
    clean = block.index("status=STORAGE_CLEAN")
    assert block.index("if waste.ok and waste.empty:") < clean < block.index('elif guard(waste, ""):')
    confirmed = block.index("status=STORAGE_CONFIRMED if _sv_ok else STORAGE_NO_READS")
    assert block.index("stmt_w = remediation.retention_fix(") < confirmed       # after the retention control
    tail = block.split("status=STORAGE_CONFIRMED if _sv_ok else STORAGE_NO_READS", 1)[1]
    assert "\n            else:\n" in tail and "status=STORAGE_SCAN_FAILED" in tail.split("\n            else:\n", 1)[1]
    assert "storage_handoff_note(st.session_state[STORAGE_HANDOFF_KEY])" in block
    assert 'empty_state("unavailable", md_dollars(S_STORAGE_LEDGER_UNAVAILABLE), detail=_st_led_err)' in block
    # the verdicts: only on the read-evidence frame, after the (locked) truncation flag
    assert '_sv_ok = reads_available and "NEVER_READ" in sdf.columns' in block
    assert "if _sv_ok:\n                    sdf = storage_waste_verdicts(sdf, rate_tb=_waste_rate_tb)" in block
    assert block.index("_wtrunc = len(sdf) >= 50") < block.index("_sv_ok = reads_available")
    assert block.index("storage_waste_verdicts(") < block.index('selectable_table(sdf, key="waste_sel"')
    # the booked-tables read: gated on a qualifying table, the unread read's SQL + tier, never a probe
    gate = "if _sv_ok and _sv_n:"
    assert block.count(gate) == 1
    led = block.split(gate, 1)[1].split("kpi_row(kpis_w)", 1)[0]
    assert 'run(mart_sql.savings_ledger(limit=None), page=_PAGE, key="booked_storage_ledger",' in led
    assert 'tier="recent"' in led and "probe=True" not in led
    assert "reclaim_" not in "booked_storage_ledger"                            # toggle_cost_hint keys on it
    assert ("booked_objects(_st_led.df, finding_types=STORAGE_BOOKED_TYPES)\n"
            "                                  if _st_led.ok and not _st_led.truncated else None") in led
    assert block.index("_st_booked: frozenset[str] | None = frozenset()") < block.index(gate)
    # v4.605 review r1: the retention control is review only. The executor's allow-list refuses ALTER TABLE (and
    # must stay that way), so its old Execute button could only log a FAILED row and its 'left out at once' branch
    # never ran: no write, no latch, no booked-set union; the ALTER is shown for a worksheet
    control = block.split("stmt_w = remediation.retention_fix(", 1)[1].split(
        "st.session_state[STORAGE_HANDOFF_KEY] = storage_handoff(", 1)[0]
    assert 'st.code(stmt_w, language="sql")' in control
    for gone in ("confirm_gate(", "write_gate_open(", "stamp_write(", "execute_statement(", "notify(",
                 "REMEDIATION_LOG", "SAVINGS_LEDGER", "_st_booked"):
        assert gone not in control, gone
    assert "execute_statement(stmt_w" not in opt and 'key="waste"' not in opt
    assert ('"Review only: OVERWATCH never runs this ALTER (ALTER TABLE is outside the in-app executor\'s '
            'allow-list). Confirm with the table\'s owner, then run it in a worksheet. The table keeps counting in '
            'Addressable $/mo until the storage-waste scan is re-run after account usage shows the change."'
            ) in _joined(control)
    assert "estimate assumes no account-level MIN_DATA_RETENTION_TIME_IN_DAYS above the retention you set" in (
        _joined(control))
    assert "left out at once" not in block and "_st_booked = _st_booked |" not in block
    # the degraded caption is worded by the error kind (this account is Enterprise), never a blanket edition claim
    assert "reads_unavailable_note(waste.error_kind, waste.error)" in block
    assert block.index("_reads_note = ") < block.index("waste = run_mart_first(")
    assert "needs Enterprise edition)" not in block
    assert 'Read evidence is unavailable: " + _reads_note' in block
    assert "st.caption(md_dollars(LEVER_LEGEND + (\" \" + FLOOR_LEGEND if _wtrunc else \"\")))" in block
    assert '"label": "Reclaimable $/mo (unread tables" + (", top 50, ≥)" if _wtrunc else ")"),' in block
    # never cleared (the toggle resets on every revisit), and no new ACCOUNT_USAGE read or write latch
    assert "pop(STORAGE_HANDOFF_KEY" not in opt and "del st.session_state[STORAGE_HANDOFF_KEY]" not in opt
    assert opt.count("ACCOUNT_USAGE") == 6
    # 7 -> 6 in the 2026-09-30 hygiene release review: the retention control's never-succeeding write is gone;
    # 6 -> 7 with R1-086: the off-hours schedule is review only, and its ESTIMATED booking is its own latched button
    assert len(re.findall(r"write_gate_open\(", opt)) == len(re.findall(r"stamp_write\(", opt)) == 7


def test_headlines_read_only_the_storage_handoff():
    opt = read("app/ui/pages/cost_parts/optimize.py")
    idle = opt.split('if opt_section == "Idle & sizing":', 1)[1].split('elif opt_section == "Queries & patterns":', 1)[0]
    rate_line = '_st_rate = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), DEFAULT_STORAGE_USD_PER_TB_MONTH)'
    call = "_storage = storage_lever(st.session_state.get(STORAGE_HANDOFF_KEY), company=company, scope=cache_scope(),"
    assert idle.count(call) == 1 and opt.count("storage_lever(") == 1
    assert idle.index(rate_line) < idle.index(call) < idle.index("_savings_opps.extend(_storage.opportunities)") < (
        idle.index("_roll = rollup_savings(_savings_opps)"))
    assert 'now=utc_now(), rate=_st_rate, where="Storage & waste")' in idle
    for token in ("storage_reclaim", "storage_waste(", "savings_ledger(", "storage_handoff("):
        assert token not in idle, token                                          # zero reads here
    assert '("STORAGE_WASTE", _storage.included)) if on]' in idle
    assert 'if not _storage.included:\n            _absent["STORAGE_WASTE"] = _storage.reason' in idle
    joined = _joined(idle)
    assert ("Double-counts dropped from the total: idle and resize on the same warehouse, or more than one of unread "
            "maintenance and storage waste on the same table (the larger counts once).") in joined
    assert 'and storage waste (" + H_STORAGE_BASIS + ") are ESTIMATED' in joined
    assert ("A table you dropped or re-set in a worksheet keeps counting until the storage-waste scan is re-run "
            "after account usage shows the change.") in joined
    ds = read("app/ui/decision_studio.py")
    pipe = ds.split("def _pipeline_tab(", 1)[1].split("\ndef ", 1)[0]
    assert pipe.count("storage_lever(") == 1 and ds.count("storage_lever(") == 1
    assert ('_st_rate = safe_float(load_settings(_PAGE).get("STORAGE_USD_PER_TB_MONTH"), '
            "DEFAULT_STORAGE_USD_PER_TB_MONTH)") in pipe
    assert pipe.index("opps.extend(_unread.opportunities)") < pipe.index("_storage = storage_lever(") < pipe.index(
        "opps.extend(_storage.opportunities)") < pipe.index("roll = rollup_savings(opps)")
    assert 'where="Cost ▸ Optimization & Savings ▸ Storage & waste")' in pipe.split("_storage = storage_lever(", 1)[1]
    assert '("STORAGE_WASTE", _storage.included)) if on]' in pipe
    door = ('if _unread.included and not _storage.included and can_open("Cost Intelligence") and st.button(\n'
            '            "Check storage waste → Cost ▸ Optimization & Savings ▸ Storage & waste",\n'
            '            key="proof_link_storage", type="tertiary"):\n'
            "        _open_storage_waste()")
    assert pipe.count(door) == 1
    assert pipe.index('key="proof_link_unread"') < pipe.index('key="proof_link_storage"')
    for token in ("storage_handoff(", "storage_waste_verdicts", "booked_objects", "STORAGE_HANDOFF_KEY] ="):
        assert token not in ds, token
    # the writer and both readers price with the SAME fallback, so the sides never disagree and fake a rate change
    for src, name in ((_waste_block(), "_waste_rate_tb"), (idle, "_st_rate"), (pipe, "_st_rate")):
        line = next(ln for ln in src.splitlines() if ln.strip().startswith(f"{name} = "))
        assert line.rstrip().endswith('"STORAGE_USD_PER_TB_MONTH"), DEFAULT_STORAGE_USD_PER_TB_MONTH)'), line
        assert "23.0" not in line, line
    joined_ds = _joined(pipe)
    assert ("and storage waste only when that section's storage-waste scan ran this session for this Company "
            "(tables nobody read in 90 days: \" + H_STORAGE_BASIS\n                         + \"). Both are "
            "ESTIMATED, \" + H_BOOKED + \"; a table counted by both counts once (the larger wins).") in joined_ds
    shell = read("app/ui/pages/decision_studio.py")
    assert "except storage waste, which is the tables' current bytes;" in _joined(shell)


def test_no_in_app_write_books_retention():
    """The storage handoff keeps the booked set read when the scan ran, like the unread one. That is safe only while
    no in-app write can book a RETENTION row in the same run. v4.605 review r1: the storage-waste retention control
    is review only (the executor's allow-list refuses ALTER TABLE, so its Execute button never succeeded and its
    booking never ran), so no in-app write books RETENTION at all. Lock the premise: 'RETENTION' is spelled as a
    SQL literal nowhere in app/, the ledger's finding-type basis is just RETENTION, and the app's one ledger REJECT
    writer never touches it."""
    spelled = sorted({str(py.relative_to(ROOT).as_posix()) for py in (ROOT / "app").rglob("*.py")
                      if "'RETENTION'" in py.read_text(encoding="utf-8")})
    assert spelled == [], spelled
    assert sorted(TABLE_FINDING_TYPES) == ["RETENTION"]
    twin = mart_sql._ledger_twin_select()
    reject_in = twin.split("AND UPPER(TRIM(m.FINDING_TYPE)) IN (", 1)[1].split(")", 1)[0]
    assert "'RETENTION'" not in reject_in and reject_in


def test_the_executor_still_refuses_the_retention_alter():
    """The control stays review only because the allow-list stays narrow: never widen it for this statement."""
    from app.core.query import _statement_allowed
    from app.logic import remediation
    ok, why = _statement_allowed(remediation.retention_fix("DB", "S", "T", 1))
    assert not ok and "outside the operator allow-list" in why
