"""v4.610.0 (owner decision 2026-10-05): OVERWATCH admins change an alert rule's THRESHOLD_NUM and ENABLED
in-app, on Alerts > Rules.

Before, rule changes were generate-only (copy the UPDATE, run it in a worksheet as SNOW_ACCOUNTADMINS /
SNOW_SYSADMINS), so a SNOW_PRI_GFR_PRD_ALFA_DSA admin -- who holds no worksheet grant on OVERWATCH -- could not
make them at all. Now an admin applies the same allow-listed UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG behind
is_operator + confirm_gate (type the RULE_ID) + the C48 latch:

* the UPDATE is a compare-and-set on each column it changes, so a stale page (or a worksheet copy run later)
  changes nothing instead of overwriting a newer edit;
* a fresh read confirms the rule now holds the new values ('conflict' otherwise: nothing changed, no audit);
* an append-only ALERT_AUDIT 'RULE_EDIT' row (INSERT only) names the viewer through identity_sql() with the old
  and new values and the UPDATE as PROOF_SQL;
* the threshold is validated (no negative, NaN / infinity, more than THRESHOLD_NUM's 4 decimals, or an
  absurd value);
* everyone else keeps the generate-only preview, and every non-admin caption names who can act now
  (config.ADMIN_ACCESS_HINT: the named admins and direct SNOW_PRI_GFR_PRD_ALFA_DSA members).
"""

from __future__ import annotations

import ast
import math

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


def _ok(df: pd.DataFrame | None = None) -> QueryResult:
    return QueryResult(df=pd.DataFrame() if df is None else df, ok=True)


# ------------------------------------------------------------------------------------------- validation ----

@pytest.mark.parametrize("value", [None, 0, 0.0, 1, 3.5, 45.0, 0.0001, 120, 1_000_000.0])
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
    (0.00001, "4 decimal"),
    (1.23456, "4 decimal"),
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
    for bad in (-1.0, float("nan"), 1e15, 0.00001):
        with pytest.raises(ValueError):
            _rule_change_sql("COST_X", 30.0, True, bad, True)


# --------------------------------------------------------------------------- compare-and-set + audit SQL ----

def test_the_update_is_a_compare_and_set_on_each_changed_column():
    from app.ui.pages.alerts import _rule_change_sql
    thr = _rule_change_sql("COST_X", 30.0, True, 45.0, True)
    assert "SET THRESHOLD_NUM = 45.0, UPDATED_AT = CURRENT_TIMESTAMP()" in thr
    assert thr.endswith("WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM = 30.0;")
    assert "ENABLED" not in thr                       # an unchanged column is neither written nor pinned
    en = _rule_change_sql("COST_X", 30.0, True, 30.0, False)
    assert "SET ENABLED = FALSE," in en and en.endswith("WHERE RULE_ID = 'COST_X' AND ENABLED = TRUE;")
    assert "THRESHOLD_NUM" not in en
    both = _rule_change_sql("COST_X", 30.0, False, 45.0, True)
    assert both.endswith("WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM = 30.0 AND ENABLED = FALSE;")
    # a NULL current threshold is pinned as NULL, never as a fabricated 0
    unset = _rule_change_sql("COST_X", None, True, 5.0, True)
    assert unset.endswith("WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM IS NULL;")


def test_the_audit_row_is_an_append_only_insert_naming_the_viewer_with_old_and_new(monkeypatch):
    from app.ui.pages import alerts
    monkeypatch.setattr(alerts, "viewer_name", lambda: "H21427")
    monkeypatch.setattr(alerts, "identity_sql", lambda: "'H21427'")
    upd = alerts._rule_change_sql("COST_X", 30.0, True, 45.0, False)
    audit = alerts._rule_audit_sql("COST_X", 30.0, True, 45.0, False)
    assert audit.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT "
                            "(EVENT_ID, ACTION, NOTE, PROOF_SQL, ACTED_BY)")
    assert "UPDATE" not in audit.split(" SELECT ", 1)[0] and "DELETE" not in audit
    assert "SELECT 'RULE:COST_X', 'RULE_EDIT', " in audit
    assert "threshold 30 -> 45" in audit and "Enabled on -> off" in audit
    assert "by H21427" in audit and audit.rstrip(";").endswith(
        "FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_X' AND THRESHOLD_NUM = 45.0 "
        "AND ENABLED = FALSE")
    assert ", 'H21427' FROM " in audit               # ACTED_BY = identity_sql(), not the owner's CURRENT_USER()
    # PROOF_SQL is the UPDATE itself (quoted), so the trail shows exactly what ran
    assert alerts.sql_literal(upd.rstrip(";"), max_len=4000) in audit
    # 'RULE:' + a 60-char RULE_ID fits ALERT_AUDIT.EVENT_ID VARCHAR(80)
    assert "EVENT_ID    VARCHAR(80)" in read("snowflake/migrations/V004__alerts.sql")


@pytest.mark.parametrize("cur_thr, cur_en, new_thr, new_en", [
    (30.0, True, 45.0, True), (30.0, True, 30.0, False), (None, False, 2.5, True), (1.0, True, 0.0, True),
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


# ------------------------------------------------------------------------------------------ apply flow ----

class _Exec:
    """Records execute_statement calls; ``fail`` maps a statement prefix to a failure message."""

    def __init__(self, fail: dict[str, str] | None = None, after_update=None) -> None:
        self.sql: list[str] = []
        self.fail = fail or {}
        self.after_update = after_update

    def __call__(self, sql, *, page):
        self.sql.append(sql)
        for prefix, msg in self.fail.items():
            if sql.startswith(prefix):
                return False, msg
        if sql.startswith("UPDATE") and self.after_update:
            self.after_update()
        return True, "Statement executed."


def _wire(monkeypatch, fresh: QueryResult, exec_: _Exec):
    from app.ui.pages import alerts
    reads: list[str] = []

    def _run(sql, **kw):
        reads.append(kw.get("key", ""))
        return fresh

    monkeypatch.setattr(alerts, "execute_statement", exec_)
    monkeypatch.setattr(alerts, "run", _run)
    return reads


def test_apply_updates_confirms_then_audits(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()
    reads = _wire(monkeypatch, _ok(_RULES.assign(THRESHOLD_NUM=[45.0, 1.0])), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "applied", msg
    assert [s.split(" ", 2)[:2] for s in ex.sql] == [["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\nSET"],
                                                    ["INSERT", "INTO"]]
    assert "ALERT_AUDIT" in ex.sql[1]
    assert reads == ["alert_rules"]                   # the confirming read is the rules read, re-keyed by the bump
    assert "COST_X" in msg and "30 -> 45" in msg


def test_a_failed_update_changes_nothing_and_writes_no_audit(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(fail={"UPDATE": "operator entitlement required"})
    reads = _wire(monkeypatch, _ok(_RULES), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "failed" and "not changed" in msg and "operator entitlement required" in msg
    assert len(ex.sql) == 1 and reads == []


def test_a_rule_edited_elsewhere_is_a_conflict_with_no_audit_row(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()   # the compare-and-set matched no row: the fresh read still shows someone else's 60
    _wire(monkeypatch, _ok(_RULES.assign(THRESHOLD_NUM=[60.0, 1.0])), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "conflict"
    assert "Nothing changed" in msg and "60" in msg
    assert len(ex.sql) == 1 and not any("ALERT_AUDIT" in s for s in ex.sql)


def test_a_rule_that_disappeared_is_a_conflict(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()
    _wire(monkeypatch, _ok(_RULES[_RULES["RULE_ID"] != "COST_X"]), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "conflict" and "no longer" in msg
    assert len(ex.sql) == 1


def test_a_failed_audit_after_a_good_update_is_partial_and_says_the_change_is_live(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec(fail={"INSERT": "insert failed"})
    _wire(monkeypatch, _ok(_RULES.assign(ENABLED=[False, True])), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 30.0, False)
    assert outcome == "partial"
    assert "ALERT_AUDIT row could not be written: insert failed" in msg and "the change is live" in msg


def test_a_failed_confirming_read_still_writes_the_self_checking_audit(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()
    _wire(monkeypatch, QueryResult(ok=False, error="timeout", error_kind="timeout"), ex)
    outcome, msg = alerts._apply_rule_change("COST_X", 30.0, True, 45.0, True)
    assert outcome == "partial" and "could not be re-read" in msg
    # the audit INSERT ... SELECT lands only while the rule holds the new values, so it is safe to send blind
    assert len(ex.sql) == 2 and "ALERT_AUDIT" in ex.sql[1] and "THRESHOLD_NUM = 45.0" in ex.sql[1]


def test_nothing_to_apply_sends_nothing(monkeypatch):
    from app.ui.pages import alerts
    ex = _Exec()
    _wire(monkeypatch, _ok(_RULES), ex)
    assert alerts._apply_rule_change("COST_X", 30.0, True, 30.0, True)[0] == "failed"
    assert ex.sql == []


# ---------------------------------------------------------------------------------------- rendered page ----

def _render_rules():
    from app.ui.pages import alerts
    alerts.render()


def _rules_app(monkeypatch, *, operator: bool, rule: str = "COST_X"):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.ui.pages import alerts
    from tests.test_alerts_failed_reads import _SECTION_RESULTS, _stub
    _stub(monkeypatch, {"alert_rules": _ok(_RULES)})
    # pin the section by stubbing the picker (see tests/test_alert_rule_generator.py: the 1.52.2 floor's
    # ButtonGroup re-serialization breaks a seeded section key on a second run)
    monkeypatch.setattr(alerts, "lazy_sections", lambda *_a, **_k: "Rules")
    monkeypatch.setattr(alerts, "_is_operator", lambda: operator)
    ex = _Exec(after_update=lambda: _SECTION_RESULTS.update(
        {"alert_rules": _ok(_RULES.assign(THRESHOLD_NUM=[45.0, 1.0]))}))
    monkeypatch.setattr(alerts, "execute_statement", ex)
    at = AppTest.from_function(_render_rules, default_timeout=30)
    at.session_state["rule_pick"] = rule
    at.run()
    assert not at.exception, at.exception
    return at, ex


def _keys(at, kind: str) -> set[str]:
    return {str(getattr(w, "key", "")) for w in getattr(at, kind)}


def test_an_admin_applies_a_threshold_change_behind_the_typed_rule_id(monkeypatch):
    at, ex = _rules_app(monkeypatch, operator=True)
    at.number_input(key="rule_thresh:COST_X").set_value(45.0).run()
    assert not at.exception, at.exception
    assert any("SET THRESHOLD_NUM = 45.0" in c.value for c in at.code)     # the preview stays
    assert "rule_apply:COST_X:0_btn" in _keys(at, "button")
    assert at.button(key="rule_apply:COST_X:0_btn").disabled                 # nothing typed yet
    at.text_input(key="rule_apply:COST_X:0_confirm").input("cost_x").run()   # exact RULE_ID, case included
    assert at.button(key="rule_apply:COST_X:0_btn").disabled
    at.text_input(key="rule_apply:COST_X:0_confirm").input("COST_X").run()
    at.button(key="rule_apply:COST_X:0_btn").click().run()
    assert not at.exception, at.exception
    assert [s.split(" ", 1)[0] for s in ex.sql] == ["UPDATE", "INSERT"]
    assert "AND THRESHOLD_NUM = 30.0" in ex.sql[0] and "'RULE_EDIT'" in ex.sql[1]
    assert any("COST_X updated" in s.value for s in at.success)            # the receipt survives the rerun
    assert at.session_state["_ow_rule_nonce"] == 1                         # the typed RULE_ID does not carry over
    # the box now equals the re-read rule, so there is nothing left to apply
    assert not any(k.startswith("rule_apply:") for k in _keys(at, "button"))


def test_a_non_admin_gets_the_preview_and_who_can_act_but_no_apply(monkeypatch):
    at, ex = _rules_app(monkeypatch, operator=False)
    at.number_input(key="rule_thresh:COST_X").set_value(45.0).run()
    assert not at.exception, at.exception
    assert any("SET THRESHOLD_NUM = 45.0" in c.value for c in at.code)
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
    assert "rule_apply:COST_X:0_btn" not in _keys(at, "button")
    assert ex.sql == []


# ------------------------------------------------------------------------------------------ source locks ----

def _rules_block() -> str:
    src = read("app/ui/pages/alerts.py")
    return src.split('elif section == "Rules":', 1)[1].split('elif section == "History":', 1)[0]


def test_the_apply_block_is_gated_confirmed_and_latched():
    src = read("app/ui/pages/alerts.py")
    block = _rules_block()
    gate = block.split("if is_operator:", 1)[1]
    assert 'confirm_gate(rule_id, "Apply rule change + audit",' in gate
    assert 'key=f"rule_apply:{rule_id}:{_rule_nonce}",' in gate     # F51: re-keyed after every apply
    assert 'st.session_state["_ow_rule_nonce"] = _rule_nonce + 1' in gate
    # the latch is the gate's LAST condition, scoped by rule and values; the stamp precedes the rerun
    assert "and write_gate_open(_rule_latch)):" in gate
    assert '_rule_latch = f"rule_apply:{rule_id}:{new_threshold}:{int(bool(enabled))}"' in gate
    tail = gate.split("and write_gate_open(_rule_latch)):", 1)[1]
    assert tail.index("_apply_rule_change(") < tail.index("stamp_write(_rule_latch,") < tail.index("st.rerun()")
    # the module docstring no longer claims rule changes are generate-only for everyone
    assert "Rule changes are generate-only by design." not in src


def test_non_admin_captions_name_who_can_act_now():
    # Every in-app "who may do this" caption on Alerts and on the Emergency lever reads config.ADMIN_ACCESS_HINT
    # (the named admins + direct SNOW_PRI_GFR_PRD_ALFA_DSA members) instead of the allowlist alone, or a
    # SNOW_* role that grants nothing in-app.
    assert "OPERATOR_USERS" in ADMIN_ACCESS_HINT and ADMIN_ACCESS_ROLE in ADMIN_ACCESS_HINT
    for rel, n in (("app/ui/pages/alerts.py", 3), ("app/ui/pages/operations.py", 1),
                   ("app/ui/pages/cost_parts/optimize.py", 2)):
        tree = ast.parse(read(rel))
        hinted = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "caption" and node.args
                  and any(isinstance(sub, ast.Name) and sub.id == "ADMIN_ACCESS_HINT"
                          for sub in ast.walk(node.args[0]))]
        assert len(hinted) >= n, (rel, len(hinted))
        src = read(rel)
        assert "limited to operators (config OPERATOR_USERS)" not in src, rel
        assert "executing from the app requires SNOW_" not in src, rel
        assert "Booking needs SNOW_" not in src, rel


def test_worksheet_captions_that_stay_true_are_kept():
    # running generate-only SQL OUTSIDE the app still needs a SNOW_* role (DSA gets no worksheet grants)
    src = read("app/ui/pages/alerts.py")
    assert 'st.caption("Copy the SQL; executing needs SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")' in src
    assert "run it in a worksheet as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS" in _rules_block()


def test_threshold_formatting_never_prints_a_float_artifact():
    from app.ui.pages.alerts import _threshold_text
    assert _threshold_text(None) == "unset"
    assert _threshold_text(45.0) == "45" and _threshold_text(1.1) == "1.1" and _threshold_text(0.0001) == "0.0001"
    assert _threshold_text(1_000_000.0) == "1000000"
    assert not math.isnan(float(_threshold_text(3.5)))
