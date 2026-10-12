"""v4.610.0 (owner decision 2026-10-05): OVERWATCH admins change an alert rule's THRESHOLD_NUM and ENABLED
in-app, on Alerts > Rules.

Before, rule changes were generate-only (copy the UPDATE, run it in a worksheet as SNOW_ACCOUNTADMINS /
SNOW_SYSADMINS), so a SNOW_PRI_GFR_PRD_ALFA_DSA admin -- who holds no worksheet grant on OVERWATCH -- could not
make them at all. Now an admin applies the same allow-listed UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG behind
is_operator + confirm_gate (type the RULE_ID) + the C48 latch:

* the UPDATE is a compare-and-set on each column it changes, so a stale page (or a worksheet copy run later)
  changes nothing instead of overwriting a newer edit; Snowflake's own row count says whether it matched
  (0 = 'conflict': nothing changed, no audit row -- an identical concurrent edit included);
* an append-only ALERT_AUDIT 'RULE_EDIT' row (INSERT only) names the viewer through identity_sql() with the old
  and new values and the UPDATE as PROOF_SQL; 'audited' is claimed only when that INSERT reports a row;
* the threshold is validated (no negative, NaN / infinity, more than THRESHOLD_NUM's 4 decimals, below its
  0.0001 step, or an absurd value) and normalised ONCE to the NUMBER(18,4) value the column stores, so the
  UPDATE, its compare-and-set, the audit hold and the confirming read all carry the same literal (the number
  box's step-down from 1.10 yields 0.10000000000000009);
* the typed RULE_ID is bound to the values it confirms: editing the threshold or Enabled clears it;
* a rule whose scan never reads THRESHOLD_NUM offers only Enabled (locked against the latest scan procs);
* a SECURITY rule switched off or loosened warns first, and every edit is listed under Recent rule changes;
* everyone else keeps the generate-only preview, and every non-admin caption names who can act now
  (config.ADMIN_ACCESS_HINT: direct SNOW_PRI_GFR_PRD_ALFA_DSA members).
"""

from __future__ import annotations

import ast
import math
import re

import pandas as pd
import pytest

import app.core.query as q
from app.config import ADMIN_ACCESS_HINT, ADMIN_ACCESS_ROLE
from app.core.result import QueryResult
from tests._source import read

_RULES = pd.DataFrame({"RULE_ID": ["COST_X", "SEC_TRUST_REGRESSION"], "FAMILY": ["COST", "SECURITY"],
                       "NAME": ["x", "trust"], "ENABLED": [True, True], "SEVERITY": ["HIGH", "HIGH"],
                       "THRESHOLD_NUM": [30.0, 1.0], "WINDOW_HOURS": [24, 24], "OWNER": ["", ""],
                       "CHANNEL": ["", ""], "UPDATED_AT": [pd.Timestamp("2026-09-01")] * 2})

# the number box steps by 1.0 with no rounding (Streamlit 1.52.2 NumberInput: value - step), so a fractional
# threshold such as COST_BUDGET_PACE's seeded 1.10 steps down to a binary artifact
_STEP_DOWN_FROM_1_1 = 1.1 - 1.0          # 0.10000000000000009
_STEP_DOWN_FROM_2_3 = 2.3 - 1.0          # 1.2999999999999998


def _ok(df: pd.DataFrame | None = None) -> QueryResult:
    return QueryResult(df=pd.DataFrame() if df is None else df, ok=True)


# ------------------------------------------------------------------------------------------- validation ----

@pytest.mark.parametrize("value", [None, 0, 0.0, 1, 3.5, 45.0, 0.0001, 120, 1_000_000.0,
                                   _STEP_DOWN_FROM_1_1, _STEP_DOWN_FROM_2_3, 0.1 + 0.2])
def test_plausible_thresholds_pass(value):
    from app.ui.pages.alerts import _rule_threshold_problem
    assert _rule_threshold_problem(value) == ""


@pytest.mark.parametrize("value, needle", [
    (-1.0, "negative"),
    (-0.0001, "negative"),
    (float("nan"), "finite"),
    (float("inf"), "finite"),
    (float("-inf"), "finite"),
    (1_000_000.5, "plausible"),
    (1e15, "plausible"),
    (1e30, "plausible"),
    (0.00001, "4 decimal"),
    (0.00004, "4 decimal"),
    (1.23456, "4 decimal"),
    (1e-10, "stored as 0"),             # within float noise of 0.0000: NUMBER(18,4) would store 0
    (1e-12, "stored as 0"),
    ("abc", "number"),
    (True, "number"),
])
def test_bad_thresholds_are_refused_with_a_reason(value, needle):
    from app.ui.pages.alerts import _rule_threshold_problem
    assert needle in _rule_threshold_problem(value)


def test_the_cap_is_far_above_every_seeded_threshold_and_inside_the_column():
    from app.ui.pages import alerts
    # ALERT_CONFIG.THRESHOLD_NUM is NUMBER(18,4): 14 integer digits, 4 decimals
    v004 = read("snowflake/migrations/V004__alerts.sql")
    assert "THRESHOLD_NUM NUMBER(18,4)" in v004
    assert alerts.RULE_THRESHOLD_MAX < 10 ** 14
    assert alerts._RULE_THRESHOLD_DECIMALS == 4


def test_the_change_builder_refuses_an_invalid_threshold():
    from app.ui.pages.alerts import _rule_change_sql
    for bad in (-1.0, float("nan"), 1e15, 0.00001, 1e-10):
        with pytest.raises(ValueError):
            _rule_change_sql("COST_X", 30.0, True, bad, True)


@pytest.mark.parametrize("raw, literal", [(_STEP_DOWN_FROM_1_1, "0.1000"), (_STEP_DOWN_FROM_2_3, "1.3000"),
                                          (0.1 + 0.2, "0.3000"), (45.0, "45.0000"), (0.0001, "0.0001"),
                                          (0, "0.0000"), (-0.0, "0.0000"), (1_000_000.0, "1000000.0000")])
def test_the_threshold_is_normalised_once_to_the_value_the_column_stores(raw, literal):
    from app.ui.pages import alerts
    assert alerts._threshold_sql(raw) == literal
    assert str(alerts._threshold_decimal(raw)) == literal


def test_a_float_artifact_writes_and_audits_the_value_the_column_stores(monkeypatch):
    # review r1 on the rule-edit commit: the 4-decimal check tolerated the artifact, then the raw repr went into
    # SET / the audit hold; NUMBER(18,4) stored 0.1000, so the INSERT ... SELECT pinned on 0.10000000000000009
    # matched no row and the receipt still said 'audited'
    from app.ui.pages import alerts
    monkeypatch.setattr(alerts, "viewer_name", lambda: "DSA_PERSON1")
    monkeypatch.setattr(alerts, "identity_sql", lambda: "'DSA_PERSON1'")
    for raw, literal in ((_STEP_DOWN_FROM_1_1, "0.1000"), (_STEP_DOWN_FROM_2_3, "1.3000")):
        upd = alerts._rule_change_sql("COST_BUDGET_PACE", 1.1, True, raw, True)
        audit = alerts._rule_audit_sql("COST_BUDGET_PACE", 1.1, True, raw, True)
        assert f"SET THRESHOLD_NUM = {literal}, UPDATED_AT" in upd
        assert upd.endswith("WHERE RULE_ID = 'COST_BUDGET_PACE' AND THRESHOLD_NUM = 1.1000;")
        assert audit.rstrip(";").endswith(f"WHERE RULE_ID = 'COST_BUDGET_PACE' AND THRESHOLD_NUM = {literal}")
        assert repr(raw) not in upd + audit
        assert f"threshold 1.1 -> {literal.rstrip('0').rstrip('.')}" in audit
    # an artifact of the CURRENT value is no change at all
    assert alerts._rule_change_sql("COST_BUDGET_PACE", 0.1, True, _STEP_DOWN_FROM_1_1, True) == ""
    assert alerts._rule_changes(0.1, True, _STEP_DOWN_FROM_1_1, True) == (False, False)


# --------------------------------------------------------------------------- compare-and-set + audit SQL ----

def test_the_update_is_a_compare_and_set_on_each_changed_column():
    from app.ui.pages.alerts import _rule_change_sql
    thr = _rule_change_sql("COST_X", 30.0, True, 45.0, True)
    assert "SET THRESHOLD_NUM = 45.0000, UPDATED_AT = CURRENT_TIMESTAMP()" in thr
    assert thr.endswith("WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM = 30.0000;")
    assert "ENABLED" not in thr                       # an unchanged column is neither written nor pinned
    en = _rule_change_sql("COST_X", 30.0, True, 30.0, False)
    assert "SET ENABLED = FALSE," in en and en.endswith("WHERE RULE_ID = 'COST_X' AND ENABLED = TRUE;")
    assert "THRESHOLD_NUM" not in en
    both = _rule_change_sql("COST_X", 30.0, False, 45.0, True)
    assert both.endswith("WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM = 30.0000 AND ENABLED = FALSE;")
    # a NULL current threshold is pinned as NULL, never as a fabricated 0
    unset = _rule_change_sql("COST_X", None, True, 5.0, True)
    assert unset.endswith("WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM IS NULL;")


def test_the_audit_row_is_an_append_only_insert_naming_the_viewer_with_old_and_new(monkeypatch):
    from app.ui.pages import alerts
    monkeypatch.setattr(alerts, "viewer_name", lambda: "DSA_PERSON1")
    monkeypatch.setattr(alerts, "identity_sql", lambda: "'DSA_PERSON1'")
    upd = alerts._rule_change_sql("COST_X", 30.0, True, 45.0, False)
    audit = alerts._rule_audit_sql("COST_X", 30.0, True, 45.0, False)
    assert audit.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT "
                            "(EVENT_ID, ACTION, NOTE, PROOF_SQL, ACTED_BY)")
    assert "UPDATE" not in audit.split(" SELECT ", 1)[0] and "DELETE" not in audit
    assert "SELECT 'RULE:COST_X', 'RULE_EDIT', " in audit
    assert "threshold 30 -> 45" in audit and "Enabled on -> off" in audit
    assert "by DSA_PERSON1" in audit and audit.rstrip(";").endswith(
        "FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM = 45.0000 "
        "AND ENABLED = FALSE")
    assert ", 'DSA_PERSON1' FROM " in audit               # ACTED_BY = identity_sql(), not the owner's CURRENT_USER()
    # PROOF_SQL is the UPDATE itself (quoted), so the trail shows exactly what ran
    assert alerts.sql_literal(upd.rstrip(";"), max_len=4000) in audit
    # 'RULE:' + a 60-char RULE_ID fits ALERT_AUDIT.EVENT_ID VARCHAR(80)
    assert "EVENT_ID    VARCHAR(80)" in read("snowflake/migrations/V004__alerts.sql")


@pytest.mark.parametrize("cur_thr, cur_en, new_thr, new_en", [
    (30.0, True, 45.0, True), (30.0, True, 30.0, False), (None, False, 2.5, True), (1.0, True, 0.0, True),
    (1.1, True, _STEP_DOWN_FROM_1_1, True),
])
def test_both_statements_pass_the_executor_allow_list_and_need_entitlement(cur_thr, cur_en, new_thr, new_en):
    from app.ui.pages import alerts
    upd = alerts._rule_change_sql("SEC_TRUST_REGRESSION", cur_thr, cur_en, new_thr, new_en)
    audit = alerts._rule_audit_sql("SEC_TRUST_REGRESSION", cur_thr, cur_en, new_thr, new_en)
    for sql in (upd, audit):
        assert q._statement_allowed(sql) == (True, ""), sql
        assert q._is_privileged(sql) is True, sql     # admin-only at the executor too, not just the UI


def test_a_note_with_quotes_and_semicolons_stays_one_statement(monkeypatch):
    from app.ui.pages import alerts
    monkeypatch.setattr(alerts, "viewer_name", lambda: "O'BRIEN;X")
    monkeypatch.setattr(alerts, "identity_sql", lambda: "'O''BRIEN;X'")
    audit = alerts._rule_audit_sql("x'y", 1.0, True, 2.0, True)
    assert q._statement_allowed(audit) == (True, "")


# ------------------------------------------------------------------------- the executor's row count ----

class _Row(tuple):
    """A Snowpark Row stand-in: positional access, like Row(number of rows updated=1, ...)."""


@pytest.mark.parametrize("rows, count", [
    ([_Row((1, 0))], 1), ([_Row((0, 0))], 0), ([_Row((3,))], 3), ([_Row(("1",))], 1),
    ([_Row(("Statement executed successfully.",))], None), ([_Row((True,))], None),
    ([], None), (None, None), ([{"ANSWER": "ok"}], None),
])
def test_the_dml_row_count_reads_the_first_result_column(rows, count):
    assert q._dml_row_count(rows) == count


def test_execute_statement_count_returns_snowflakes_row_count_through_the_same_gates(monkeypatch):
    from tests.test_statement_params import _Sis, _Stmt
    stmt = _Stmt(rows=[_Row((0, 0))])
    monkeypatch.setattr(q, "get_session", lambda: _Sis(stmt))
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q, "_bump_refresh", lambda *_a, **_k: None)
    seen: list[tuple[str, str]] = []

    def _refusal(sql, *, page, seam, **_k):
        seen.append((seam, sql))
        return ""

    monkeypatch.setattr(q, "_entitlement_refusal", _refusal)
    upd = "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG SET ENABLED = FALSE WHERE RULE_ID = 'X' AND ENABLED = TRUE;"
    assert q.execute_statement_count(upd, page="Alerts") == (True, "Statement executed.", 0)
    assert seen == [("execute_statement", upd)]       # the same entitlement seam execute_statement crosses
    assert q.execute_statement(upd, page="Alerts") == (True, "Statement executed.")   # unchanged 2-tuple
    # the allow-list and the entitlement refusal still stop it before Snowflake
    assert q.execute_statement_count("DROP TABLE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG;", page="x")[::2] == (False, None)
    monkeypatch.setattr(q, "_entitlement_refusal", lambda *_a, **_k: "operator entitlement required")
    assert q.execute_statement_count(upd, page="x") == (False, "operator entitlement required", None)


# ------------------------------------------------------------------------------------------ apply flow ----

class _Exec:
    """Records execute_statement_count calls. ``fail`` maps a statement prefix to a failure message; ``counts``
    maps a statement keyword (UPDATE / INSERT) to the row count Snowflake reports (default 1; None = no count)."""

    def __init__(self, fail: dict[str, str] | None = None, after_update=None,
                 counts: dict[str, int | None] | None = None) -> None:
        self.sql: list[str] = []
        self.fail = fail or {}
        self.after_update = after_update
        self.counts = {"UPDATE": 1, "INSERT": 1} if counts is None else counts

    def __call__(self, sql, *, page):
        self.sql.append(sql)
        for prefix, msg in self.fail.items():
            if sql.startswith(prefix):
                return False, msg, None
        if sql.startswith("UPDATE") and self.after_update:
            self.after_update()
        return True, "Statement executed.", self.counts.get(sql.split(" ", 1)[0], 1)


def _wire(monkeypatch, fresh: QueryResult, exec_: _Exec):
    from app.ui.pages import alerts
    reads: list[str] = []

    def _run(sql, **kw):
        reads.append(kw.get("key", ""))
        return fresh

    monkeypatch.setattr(alerts, "execute_statement_count", exec_)
    monkeypatch.setattr(alerts, "run", _run)
    return reads


def test_apply_updates_then_audits_on_snowflakes_row_counts(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()
    reads = _wire(monkeypatch, _ok(_RULES.assign(THRESHOLD_NUM=[45.0, 1.0])), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "applied", msg
    assert [s.split(" ", 2)[:2] for s in ex.sql] == [["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\nSET"],
                                                    ["INSERT", "INTO"]]
    assert "ALERT_AUDIT" in ex.sql[1]
    assert reads == []        # the UPDATE's own row count proves the change; the page rerun re-reads the table
    assert "COST_X" in msg and "30 -> 45" in msg and "audited" in msg


def test_a_failed_update_changes_nothing_and_writes_no_audit(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(fail={"UPDATE": "operator entitlement required"})
    reads = _wire(monkeypatch, _ok(_RULES), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "failed" and "not changed" in msg and "operator entitlement required" in msg
    assert len(ex.sql) == 1 and reads == []


def test_a_rule_edited_elsewhere_is_a_conflict_with_no_audit_row(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(counts={"UPDATE": 0})   # the compare-and-set matched no row: the rule now reads someone else's 60
    reads = _wire(monkeypatch, _ok(_RULES.assign(THRESHOLD_NUM=[60.0, 1.0])), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "conflict"
    assert "Nothing changed" in msg and "60" in msg
    assert len(ex.sql) == 1 and not any("ALERT_AUDIT" in s for s in ex.sql)
    assert reads == ["alert_rules"]                   # read only to say what the rule holds now


def test_an_identical_concurrent_edit_is_a_conflict_not_a_second_audit_row(monkeypatch):
    # review r1: admin A and admin B (or one admin in two tabs) both apply 30 -> 45. B's compare-and-set is pinned
    # on the stale 30 and matches nothing, yet a fresh read shows 45 = B's new value -- the read alone counted it
    # as held and B's INSERT ... SELECT landed a second, false RULE_EDIT row. Snowflake's row count decides now.
    from app.ui.pages import alerts
    ex = _Exec(counts={"UPDATE": 0})
    _wire(monkeypatch, _ok(_RULES.assign(THRESHOLD_NUM=[45.0, 1.0])), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "conflict", msg
    assert "already holds" in msg and "no audit row" in msg
    assert len(ex.sql) == 1 and not any("ALERT_AUDIT" in s for s in ex.sql)


def test_a_rule_that_disappeared_is_a_conflict(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(counts={"UPDATE": 0})
    _wire(monkeypatch, _ok(_RULES[_RULES["RULE_ID"] != "COST_X"]), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "conflict" and "no longer" in msg
    assert len(ex.sql) == 1


def test_a_conflict_whose_read_fails_still_says_nothing_changed(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(counts={"UPDATE": 0})
    _wire(monkeypatch, QueryResult(ok=False, error="timeout", error_kind="timeout"), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "conflict" and "Nothing changed" in msg
    assert len(ex.sql) == 1


def test_a_failed_audit_after_a_good_update_is_partial_and_says_the_change_is_live(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(fail={"INSERT": "insert failed"})
    _wire(monkeypatch, _ok(_RULES.assign(ENABLED=[False, True])), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 30.0, False)
    assert outcome == "partial"
    assert "ALERT_AUDIT row could not be written: insert failed" in msg and "the change is live" in msg


def test_an_audit_insert_that_landed_no_row_is_partial_never_audited(monkeypatch):
    # the INSERT ... SELECT holds the NEW values: when the rule moved again between the UPDATE and the audit, it
    # inserts nothing and Snowflake still reports success -- the receipt must not claim an audit row
    from app.ui.pages import alerts
    ex = _Exec(counts={"UPDATE": 1, "INSERT": 0})
    _wire(monkeypatch, _ok(_RULES), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "partial", msg
    assert "was not written" in msg and "and audited" not in msg


@pytest.mark.parametrize("fresh_thr, outcome", [(45.0, "applied"), (60.0, "conflict")])
def test_without_a_row_count_a_fresh_read_decides(monkeypatch, fresh_thr, outcome):
    from app.ui.pages import alerts
    ex = _Exec(counts={"UPDATE": None, "INSERT": None})
    reads = _wire(monkeypatch, _ok(_RULES.assign(THRESHOLD_NUM=[fresh_thr, 1.0])), ex)
    got, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert got == outcome, msg
    assert reads == ["alert_rules"]
    assert ("ALERT_AUDIT" in ex.sql[-1]) is (outcome == "applied")


def test_without_a_row_count_the_read_compares_the_stored_value(monkeypatch):
    # the column returns 0.1 for a step-down artifact: the same normalised value, so the change is held
    from app.ui.pages import alerts
    rules = _RULES.assign(RULE_ID=["COST_BUDGET_PACE", "SEC_TRUST_REGRESSION"], THRESHOLD_NUM=[0.1, 1.0])
    ex = _Exec(counts={"UPDATE": None, "INSERT": None})
    _wire(monkeypatch, _ok(rules), ex)
    outcome, msg = alerts._apply_rule_change("COST_BUDGET_PACE", 1.1, True, _STEP_DOWN_FROM_1_1, True)
    assert outcome == "applied", msg
    assert "THRESHOLD_NUM = 0.1000" in ex.sql[1]


def test_a_failed_confirming_read_without_a_row_count_still_writes_the_self_checking_audit(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(counts={"UPDATE": None, "INSERT": None})
    _wire(monkeypatch, QueryResult(ok=False, error="timeout", error_kind="timeout"), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "partial" and "could not be re-read" in msg
    # the audit INSERT ... SELECT lands only while the rule holds the new values, so it is safe to send blind
    assert len(ex.sql) == 2 and "ALERT_AUDIT" in ex.sql[1] and "THRESHOLD_NUM = 45.0000" in ex.sql[1]


def test_nothing_to_apply_sends_nothing(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()
    _wire(monkeypatch, _ok(_RULES), ex)
    assert alerts._apply_rule_change("COST_X", 30.0, True, 30.0, True)[0] == "failed"
    assert ex.sql == []


# ------------------------------------------------------------- rules whose scan never reads THRESHOLD_NUM ----

def _threshold_statements() -> tuple[set[str], set[str]]:
    """(rules some latest proc statement reads THRESHOLD_NUM beside, live rules): a statement = a ';'-split at
    parenthesis depth 0 of a latest procedure body; THRESHOLD_NUM counts only as code (comments and strings
    masked), the rule id as the quoted literal it is. A rule read through a variable (SELECT THRESHOLD_NUM INTO
    :n ... WHERE RULE_ID = 'X') counts: that statement names both."""
    from tests.test_alert_rule_consistency import (
        _RULE_RE,
        _config_deleted,
        _config_seeded_ever,
        _latest_proc_bodies,
        _raised_rule_ids,
    )
    from tests.test_sql_division_guards import mask
    reads: set[str] = set()
    for body in _latest_proc_bodies().values():
        masked, start, depth = mask(body), 0, 0
        for i, ch in enumerate(masked + ";"):
            depth += (ch == "(") - (ch == ")")
            if ch == ";" and depth == 0:
                if "THRESHOLD_NUM" in masked[start:i]:
                    reads |= set(_RULE_RE.findall(body[start:i]))
                start = i + 1
    return reads, _raised_rule_ids() & (_config_seeded_ever() - _config_deleted())


def test_threshold_informational_rules_are_exactly_the_live_rules_no_scan_reads_it_for():
    # review r1: SEC_ADMIN_GRANT's arm reads only 'ALERT_CONFIG WHERE ENABLED' and raises one event per grant, so a
    # threshold edit was applied, audited and receipted 'the next scan uses it' while nothing changed (law 8)
    from app.ui.pages import alerts
    reads, live = _threshold_statements()
    assert len(reads & live) >= 30, sorted(reads & live)                # reach: the lint sees the scans
    assert reads >= {"COST_DAILY_CREDITS", "SEC_TRUST_REGRESSION", "SEC_LOGIN_TAKEOVER", "DQ_RECON_ERROR"}
    assert frozenset(live - reads) == alerts.THRESHOLD_INFORMATIONAL_RULES, sorted(live - reads)
    assert "SEC_ADMIN_GRANT" in alerts.THRESHOLD_INFORMATIONAL_RULES


def test_a_threshold_change_to_an_informational_rule_never_claims_the_scan_uses_it(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()
    _wire(monkeypatch, _ok(_RULES), ex)
    outcome, msg = alerts._apply_rule_change("SEC_ADMIN_GRANT", 1.0, True, 5.0, True)
    assert outcome == "applied", msg
    assert "next scan uses it" not in msg and "does not read THRESHOLD_NUM" in msg


# ---------------------------------------------------------------------------------- recent rule changes ----

def test_the_rule_edit_reader_reads_only_rule_edit_audit_rows():
    from app.data import canary, mart_sql
    sql = mart_sql.alert_rule_edits(20)
    assert "FROM DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT" in sql and "WHERE ACTION = 'RULE_EDIT'" in sql
    assert "ORDER BY ACTED_AT DESC" in sql and sql.rstrip().endswith("LIMIT 20")
    assert mart_sql.alert_rule_edits(10_000).rstrip().endswith("LIMIT 200")   # capped
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, read="snowflake")
    assert "mart.alert_rule_edits" in {name for name, _fn in canary.CANARIES}  # law 4


# ---------------------------------------------------------------------------------------- rendered page ----

def _render_rules():
    from app.ui.pages import alerts
    alerts.render()


def _rules_app(monkeypatch, *, operator: bool, rule: str = "COST_X", rules: pd.DataFrame | None = None,
               extra: dict[str, QueryResult] | None = None):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.ui.pages import alerts
    from tests.test_alerts_failed_reads import _SECTION_RESULTS, _stub
    base = _RULES if rules is None else rules
    _stub(monkeypatch, {"alert_rules": _ok(base), **(extra or {})})
    # pin the section by stubbing the picker (see tests/test_alert_rule_generator.py: the 1.52.2 floor's
    # ButtonGroup re-serialization breaks a seeded section key on a second run)
    monkeypatch.setattr(alerts, "lazy_sections", lambda *_a, **_k: "Rules")
    monkeypatch.setattr(alerts, "_is_operator", lambda: operator)
    ex = _Exec(after_update=lambda: _SECTION_RESULTS.update(
        {"alert_rules": _ok(base.assign(THRESHOLD_NUM=[45.0, *list(base["THRESHOLD_NUM"])[1:]]))}))
    monkeypatch.setattr(alerts, "execute_statement_count", ex)
    at = AppTest.from_function(_render_rules, default_timeout=30)
    at.session_state["rule_pick"] = rule
    at.run()
    assert not at.exception, at.exception
    return at, ex


def _keys(at, kind: str) -> set[str]:
    return {str(getattr(w, "key", "")) for w in getattr(at, kind)}


def test_the_confirm_scope_carries_the_rule_and_the_normalised_values():
    from app.ui.pages.alerts import _rule_value_scope
    assert _rule_value_scope("COST_X", 45.0, True) == "rule_apply:COST_X:45.0000:1"
    assert _rule_value_scope("COST_X", _STEP_DOWN_FROM_1_1, False) == "rule_apply:COST_X:0.1000:0"
    assert _rule_value_scope("COST_X", None, True) == "rule_apply:COST_X:keep:1"


def test_an_admin_applies_a_threshold_change_behind_the_typed_rule_id(monkeypatch):
    at, ex = _rules_app(monkeypatch, operator=True)
    at.number_input(key="rule_thresh:COST_X").set_value(45.0).run()
    assert not at.exception, at.exception
    assert any("SET THRESHOLD_NUM = 45.0000" in c.value for c in at.code)   # the preview stays
    btn = "rule_apply:COST_X:45.0000:1:0"
    assert f"{btn}_btn" in _keys(at, "button")
    assert at.button(key=f"{btn}_btn").disabled                             # nothing typed yet
    at.text_input(key=f"{btn}_confirm").input("cost_x").run()               # exact RULE_ID, case included
    assert at.button(key=f"{btn}_btn").disabled
    at.text_input(key=f"{btn}_confirm").input("COST_X").run()
    at.button(key=f"{btn}_btn").click().run()
    assert not at.exception, at.exception
    assert [s.split(" ", 1)[0] for s in ex.sql] == ["UPDATE", "INSERT"]
    assert "AND THRESHOLD_NUM = 30.0000" in ex.sql[0] and "'RULE_EDIT'" in ex.sql[1]
    assert any("COST_X updated" in s.value for s in at.success)            # the receipt survives the rerun
    assert at.session_state["_ow_rule_nonce"] == 1                         # the typed RULE_ID does not carry over
    # the box now equals the re-read rule, so there is nothing left to apply
    assert not any(k.startswith("rule_apply:") for k in _keys(at, "button"))


def test_the_typed_rule_id_does_not_survive_a_value_edit(monkeypatch):
    # review r1: the confirm was keyed rule_apply:{rule}:{nonce}, so a typed RULE_ID survived a later threshold edit
    # and Apply sent a value the preview never showed (set 45, type COST_X, set 900, click -> 900). The confirm is
    # now keyed by the values it confirms.
    at, ex = _rules_app(monkeypatch, operator=True)
    at.number_input(key="rule_thresh:COST_X").set_value(45.0).run()
    at.text_input(key="rule_apply:COST_X:45.0000:1:0_confirm").input("COST_X").run()
    assert not at.button(key="rule_apply:COST_X:45.0000:1:0_btn").disabled
    at.number_input(key="rule_thresh:COST_X").set_value(900.0).run()
    assert not at.exception, at.exception
    assert "rule_apply:COST_X:45.0000:1:0_btn" not in _keys(at, "button")
    assert at.button(key="rule_apply:COST_X:900.0000:1:0_btn").disabled
    assert at.text_input(key="rule_apply:COST_X:900.0000:1:0_confirm").value == ""
    # an Enabled edit re-keys it too
    at.text_input(key="rule_apply:COST_X:900.0000:1:0_confirm").input("COST_X").run()
    at.checkbox(key="rule_enabled:COST_X").uncheck().run()
    assert at.button(key="rule_apply:COST_X:900.0000:0:0_btn").disabled
    assert ex.sql == []


def test_a_non_admin_gets_the_preview_and_who_can_act_but_no_apply(monkeypatch):
    at, ex = _rules_app(monkeypatch, operator=False)
    at.number_input(key="rule_thresh:COST_X").set_value(45.0).run()
    assert not at.exception, at.exception
    assert any("SET THRESHOLD_NUM = 45.0000" in c.value for c in at.code)
    assert not any(k.startswith("rule_apply:") for k in _keys(at, "button") | _keys(at, "text_input"))
    captions = [c.value for c in at.caption]
    assert any(ADMIN_ACCESS_HINT in c and "generate-only" in c for c in captions)
    assert ex.sql == []


def test_an_invalid_threshold_shows_why_and_offers_no_sql_or_apply(monkeypatch):
    at, ex = _rules_app(monkeypatch, operator=True)
    at.number_input(key="rule_thresh:COST_X").set_value(5_000_000.0).run()
    assert not at.exception, at.exception
    assert any("plausible" in e.value for e in at.error)
    assert not any("ALERT_CONFIG" in c.value for c in at.code)
    assert not any(k.startswith("rule_apply:") for k in _keys(at, "button"))
    assert ex.sql == []


def test_the_number_box_step_down_previews_and_applies_the_stored_value(monkeypatch):
    # review r1's probe on the 1.52.2 floor: decrement() on a 1.10 rule rendered 'SET THRESHOLD_NUM =
    # 0.10000000000000009' and offered Apply; the preview, the confirm key and the sent UPDATE now carry 0.1000
    rules = _RULES.assign(RULE_ID=["COST_BUDGET_PACE", "SEC_TRUST_REGRESSION"], THRESHOLD_NUM=[1.1, 1.0])
    at, ex = _rules_app(monkeypatch, operator=True, rule="COST_BUDGET_PACE", rules=rules)
    at.number_input(key="rule_thresh:COST_BUDGET_PACE").decrement().run()
    assert not at.exception, at.exception
    assert not [e.value for e in at.error]
    code = next(c.value for c in at.code if "ALERT_CONFIG" in c.value)
    assert "SET THRESHOLD_NUM = 0.1000," in code and "AND THRESHOLD_NUM = 1.1000;" in code
    assert "0.10000000000000009" not in code
    btn = "rule_apply:COST_BUDGET_PACE:0.1000:1:0"
    at.text_input(key=f"{btn}_confirm").input("COST_BUDGET_PACE").run()
    at.button(key=f"{btn}_btn").click().run()
    assert not at.exception, at.exception
    assert [s.split(" ", 1)[0] for s in ex.sql] == ["UPDATE", "INSERT"]
    assert "THRESHOLD_NUM = 0.1000" in ex.sql[1] and "0.10000000000000009" not in "".join(ex.sql)


def test_a_rule_whose_scan_ignores_the_threshold_offers_only_enabled(monkeypatch):
    rules = pd.concat([_RULES, _RULES.iloc[[1]].assign(RULE_ID="SEC_ADMIN_GRANT", NAME="grant")],
                      ignore_index=True)
    at, _ex = _rules_app(monkeypatch, operator=True, rule="SEC_ADMIN_GRANT", rules=rules)
    assert at.number_input(key="rule_thresh:SEC_ADMIN_GRANT").disabled
    assert any("SEC_ADMIN_GRANT" in c.value and "does not read THRESHOLD_NUM" in c.value for c in at.caption)
    at.checkbox(key="rule_enabled:SEC_ADMIN_GRANT").uncheck().run()
    assert not at.exception, at.exception
    codes = [c.value for c in at.code if "ALERT_CONFIG" in c.value]
    assert codes and all("THRESHOLD_NUM" not in c for c in codes)            # Enabled only
    assert "rule_apply:SEC_ADMIN_GRANT:keep:0:0_btn" in _keys(at, "button")


def test_switching_off_or_loosening_a_security_rule_warns_first(monkeypatch):
    # review r1 (hardening): any admin -- the owner chose full parity for SNOW_PRI_GFR_PRD_ALFA_DSA -- may switch
    # off a SECURITY rule; the editor says what that costs before the typed confirm, and the edit is listed under
    # Recent rule changes
    at, _ex = _rules_app(monkeypatch, operator=True, rule="SEC_TRUST_REGRESSION")
    assert not any("SECURITY rule" in w.value for w in at.warning)
    at.checkbox(key="rule_enabled:SEC_TRUST_REGRESSION").uncheck().run()
    assert any("SECURITY rule" in w.value and "Recent rule changes" in w.value for w in at.warning)
    at.checkbox(key="rule_enabled:SEC_TRUST_REGRESSION").check().run()
    at.number_input(key="rule_thresh:SEC_TRUST_REGRESSION").set_value(4.0).run()
    assert any("SECURITY rule" in w.value for w in at.warning)                 # a higher bar pages less
    at.number_input(key="rule_thresh:SEC_TRUST_REGRESSION").set_value(1.0).run()
    assert not any("SECURITY rule" in w.value for w in at.warning)


def test_recent_rule_changes_list_the_audit_rows(monkeypatch):
    edits = pd.DataFrame({"ACTED_AT": [pd.Timestamp("2026-10-05 09:00")], "ACTED_BY": ["DSA_PERSON1"],
                          "RULE_ID": ["SEC_ADMIN_GRANT"],
                          "CHANGE": ["rule SEC_ADMIN_GRANT: Enabled on -> off — by DSA_PERSON1"]})
    at, _ex = _rules_app(monkeypatch, operator=False, extra={"alert_rule_edits": _ok(edits)})
    assert any("Recent rule changes" in m.value for m in at.markdown)
    frames = [d.value for d in at.dataframe]
    assert any("DSA_PERSON1" in f.astype(str).to_string() for f in frames)


def test_no_rule_changes_yet_is_a_quiet_no_data_state(monkeypatch):
    at, _ex = _rules_app(monkeypatch, operator=False)
    assert any("No in-app rule changes recorded yet" in c.value for c in at.caption)


def test_a_failed_rule_change_read_is_unavailable(monkeypatch):
    fail = QueryResult(ok=False, error="boom", error_kind="timeout")
    at, _ex = _rules_app(monkeypatch, operator=False, extra={"alert_rule_edits": fail})
    assert any("Recent rule changes could not be read." in e.value for e in at.error)


# ------------------------------------------------------------------------------------------ source locks ----

def _rules_block() -> str:
    src = read("app/ui/pages/alerts.py")
    return src.split('elif section == "Rules":', 1)[1].split('elif section == "History":', 1)[0]


def test_the_apply_block_is_gated_confirmed_and_latched():
    src = read("app/ui/pages/alerts.py")
    block = _rules_block()
    gate = block.split("if is_operator:", 1)[1]
    assert 'confirm_gate(rule_id, "Apply rule change + audit",' in gate
    # F51 + review r1: the confirm is keyed by the rule, the normalised values it confirms and a nonce the apply
    # bumps -- a value edit or a finished apply mounts a fresh, empty confirm
    assert "_rule_scope = _rule_value_scope(rule_id, _thr_input, enabled)" in gate
    assert 'key=f"{_rule_scope}:{_rule_nonce}",' in gate
    assert 'st.session_state["_ow_rule_nonce"] = _rule_nonce + 1' in gate
    # the latch is the gate's LAST condition, scoped by rule and values; the stamp precedes the rerun
    assert "and write_gate_open(_rule_scope)):" in gate
    tail = gate.split("and write_gate_open(_rule_scope)):", 1)[1]
    assert tail.index("_apply_rule_change(") < tail.index("stamp_write(_rule_scope,") < tail.index("st.rerun()")
    # the module docstring no longer claims rule changes are generate-only for everyone
    assert "Rule changes are generate-only by design." not in src


_ADMIN_HINTED = (("app/ui/pages/alerts.py", 3), ("app/ui/pages/operations.py", 1),
                 ("app/ui/pages/cost_parts/optimize.py", 3), ("app/ui/pages/admin.py", 1))


def test_non_admin_captions_name_who_can_act_now():
    # Every in-app "who may do this" caption on Alerts, Emergency, Optimization and Admin settings reads
    # config.ADMIN_ACCESS_HINT (direct SNOW_PRI_GFR_PRD_ALFA_DSA members) instead of a username list, or a
    # SNOW_* role that grants nothing in-app. v4.611.0: roles alone decide, so the hint names no username route.
    assert "OPERATOR_USERS" not in ADMIN_ACCESS_HINT and ADMIN_ACCESS_ROLE in ADMIN_ACCESS_HINT
    for rel, n in _ADMIN_HINTED:
        tree = ast.parse(read(rel))
        hinted = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "caption" and node.args
                  and any(isinstance(sub, ast.Name) and sub.id == "ADMIN_ACCESS_HINT"
                          for sub in ast.walk(node.args[0]))]
        assert len(hinted) >= n, (rel, len(hinted))
        src = read(rel)
        flat = re.sub(r'"\s*\n\s*f?"', "", src)          # implicit concatenation across lines, read whole
        assert "limited to operators (config OPERATOR_USERS)" not in flat, rel
        assert "executing from the app requires SNOW_" not in flat, rel
        assert "Booking needs SNOW_" not in flat, rel
        # review r1: the off-hours SCHEDULE caption said "booking its saving requires SNOW_..." -- an in-app
        # booking is an admin's, and a SNOW_* role alone grants none of it
        assert not re.search(r"requires SNOW_", flat), rel


def test_worksheet_captions_that_stay_true_are_kept():
    # running generate-only SQL OUTSIDE the app still needs a SNOW_* role (DSA gets no worksheet grants)
    src = read("app/ui/pages/alerts.py")
    assert 'st.caption("Copy the SQL; executing needs SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")' in src
    assert "run it in a worksheet as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS" in _rules_block()


def test_threshold_formatting_never_prints_a_float_artifact():
    from app.ui.pages.alerts import _threshold_text
    assert _threshold_text(None) == "unset"
    assert _threshold_text(45.0) == "45" and _threshold_text(1.1) == "1.1" and _threshold_text(0.0001) == "0.0001"
    assert _threshold_text(1_000_000.0) == "1000000" and _threshold_text(0) == "0"
    assert _threshold_text(_STEP_DOWN_FROM_1_1) == "0.1"
    assert not math.isnan(float(_threshold_text(3.5)))
