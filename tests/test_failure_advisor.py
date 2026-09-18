"""Locks for failure_advisor.fix_for — the Failures-by-error remediation column.

Keys on the real error families the account is seeing (Snowsight screenshots): one ERROR_CODE
carries many messages, so the map is message-substring-first and must return a concrete step.
"""

from __future__ import annotations

from app.logic import failure_advisor


def test_real_families_get_a_concrete_fix():
    cases = [
        ("090105", "Cannot perform SHOW GRANTS. This session does not have a current database.", "USE DATABASE"),
        ("099106", "There is already a live version. Please commit it first.", "commit"),
        ("002003", "SQL compilation error: Cortex Agent '' does not exist or not authorized.", "Cortex"),
        ("002003", "SQL compilation error: Database 'CORTEX_CODE' does not exist or not authorized.", "CORTEX_CODE"),
        ("002003", "SQL compilation error: Schema 'TRXS_EDW_PRD.\"SourceOPDW\"' does not exist", "schema"),
        ("001003", "SQL compilation error: syntax error line 1 at position 15 unexpected 'TASKS'.", "syntax"),
        ("001006", "SQL compilation error: invalid parameter 'GET_CATALOG_LINKED_DATABASE_C'", "argument"),
        ("003005", "SQL compilation error: View 'SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING'", "data-quality"),
        ("002043", "SQL compilation error: Object does not exist, or operation cannot be performed", "dropped"),
    ]
    for code, msg, needle in cases:
        fix = failure_advisor.fix_for(code, msg)
        assert fix, f"no fix for {code}: {msg}"
        assert needle.lower() in fix.lower(), f"fix for {msg!r} missing {needle!r}: {fix!r}"


def test_message_substring_beats_code_when_one_code_has_many_messages():
    # 002003 appears for BOTH a Cortex-agent error and a schema error — the message decides.
    cortex = failure_advisor.fix_for("002003", "Cortex Agent '' does not exist or not authorized.")
    schema = failure_advisor.fix_for("002003", "Schema 'X' does not exist")
    assert cortex != schema
    assert "Cortex" in cortex and "schema" in schema.lower()


def test_current_database_is_not_swallowed_by_generic_not_authorized():
    # "does not have a current database" must win over the generic "does not exist or not
    # authorized" rule (order: the specific session-state rule comes first).
    fix = failure_advisor.fix_for("090105", "This session does not have a current database.")
    assert "USE DATABASE" in fix


def test_timeout_matches_the_real_snowflake_message():
    # the real message is "Statement reached its statement OR WAREHOUSE timeout of N second(s)
    # and was canceled" (query.py/insights.py canonical form) — the rule must match it.
    fix = failure_advisor.fix_for(
        "000630", "Statement reached its statement or warehouse timeout of 300 second(s) and was canceled.")
    assert "STATEMENT_TIMEOUT_IN_SECONDS" in fix


def test_unknown_family_returns_blank():
    assert failure_advisor.fix_for("999999", "some brand new error nobody mapped") == ""
    assert failure_advisor.fix_for(None, None) == ""


def test_never_raises_on_odd_input():
    for code, msg in ((0, 0), (12345, ""), ("", None), (None, "syntax error somewhere")):
        assert isinstance(failure_advisor.fix_for(code, msg), str)
