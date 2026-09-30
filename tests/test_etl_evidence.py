"""Next-Fifty #14 Phase 1: the ETL task-evidence verdict (app/logic/etl_evidence.py). Pure — runs on the
floor-compat leg too. One test per verdict branch, plus the pruning floor, the task picker and the
builder <-> verdict column contract (via the shaped harness, so a rename on either side fails here)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from app.data import etl_control_sql as etl
from app.logic import etl_evidence as ev
from app.logic.etl_evidence import (
    INFORMATICA_SIDE_SENTENCE,
    evidence_display_frame,
    evidence_floor_days,
    evidence_task_labels,
    evidence_task_options,
    task_evidence_lines,
    task_first_start,
)

_TODAY = date(2026, 9, 28)
_GIB = 1024 ** 3


def _row(**over) -> dict:
    """One healthy, finished, successful CALL of SP_X with 10 statements inside it."""
    base = {
        "TASK_START_TIME": pd.Timestamp("2026-09-27 22:00"), "TASK_END_TIME": pd.Timestamp("2026-09-27 23:00"),
        "ATTEMPTS": 1, "TASK_STATUS": "SUCCEEDED", "IS_TASK_FAILED": 0, "IS_CALL_FAILED": 0,
        "TASK_WINDOW_SEC": 3600, "END_AGE_MIN": 600, "MATCHED_CALLS": 1, "FAILED_CALLS": 0,
        "ALL_CALLS_ELAPSED_MS": 3_500_000, "CALL_QUERY_ID": "01b-call",
        "CALL_START_TIME": pd.Timestamp("2026-09-27 22:01"), "EXECUTION_STATUS": "SUCCESS",
        "ERROR_CODE": None, "ERROR_MESSAGE": None, "WAREHOUSE_NAME": "WH_ETL",
        "CALL_DATABASE": "ALFA_EDW_PRD", "CONTROL_DATABASE": "ALFA_EDW_PRD", "CALL_ELAPSED_MS": 3_500_000,
        "CALL_QUEUED_MS": 0, "CHILD_STATEMENTS": 10, "FAILED_CHILD_STATEMENTS": 0, "QUEUED_OVERLOAD_MS": 0,
        "QUEUED_PROVISIONING_MS": 0, "COMPILE_MS": 1_000, "EXEC_MS": 3_000_000, "CHILD_ELAPSED_MS": 3_400_000,
        "QUEUED_PCT": 0.0, "QUEUE_WAREHOUSE": "WH_ETL", "SPILL_LOCAL_GB": 0.0, "SPILL_REMOTE_GB": 0.0,
        "FAILED_CHILD_QUERY_ID": None, "CHILD_ERROR_MESSAGE": None,
    }
    return {**base, **over}


def _lines(*rows: dict, task: str = "SP_X"):
    return task_evidence_lines(pd.DataFrame(list(rows)), task=task)


def _levels(lines) -> list[str]:
    return [line.level for line in lines]


# --- the pruning floor ----------------------------------------------------------------------------

def test_floor_days_from_timestamp_date_and_iso() -> None:
    assert evidence_floor_days(pd.Timestamp("2026-09-27 22:01"), today=_TODAY) == 3
    assert evidence_floor_days(datetime(2026, 9, 20, 1, 0), today=_TODAY) == 10
    assert evidence_floor_days(date(2026, 9, 28), today=_TODAY) == 2            # clamped up to 2
    assert evidence_floor_days("2026-09-20T01:00:00", today=_TODAY) == 10
    assert evidence_floor_days("2026-09-20 01:00:00", today=_TODAY) == 10
    assert evidence_floor_days(date(2020, 1, 1), today=_TODAY) == 366           # clamped to QH retention
    assert evidence_floor_days(date(2026, 10, 5), today=_TODAY) == 2            # a future start still floors


@pytest.mark.parametrize("bad", [1.0, 7, None, pd.NaT, float("nan"), "", "garbage", [1, 2]])
def test_floor_days_degrades_to_no_floor(bad) -> None:
    assert evidence_floor_days(bad, today=_TODAY) == 0


def test_floor_days_defaults_to_account_today(monkeypatch) -> None:
    monkeypatch.setattr(ev, "account_today", lambda: date(2026, 9, 30))
    assert evidence_floor_days(date(2026, 9, 27)) == 5                           # never the server clock


def test_task_first_start_takes_the_earliest_attempt() -> None:
    df = pd.DataFrame({"TASK_NAME": ["SP_A", "SP_A", "SP_B"],
                       "TASK_START_DTTM": [pd.Timestamp("2026-09-27 23:00"), pd.Timestamp("2026-09-27 22:00"),
                                           pd.Timestamp("2026-09-20 22:00")]})
    assert task_first_start(df, "SP_A") == date(2026, 9, 27)
    assert task_first_start(df, "SP_B") == date(2026, 9, 20)
    assert task_first_start(df, "SP_Z") is None
    assert task_first_start(pd.DataFrame({"TASK_NAME": ["SP_A"], "TASK_START_DTTM": [1.0]}), "SP_A") is None
    assert task_first_start(pd.DataFrame({"TASK_NAME": ["SP_A"]}), "SP_A") is None
    assert task_first_start(None, "SP_A") is None


# --- the task picker --------------------------------------------------------------------------------

def test_task_options_failed_first_then_slowest_and_dedup() -> None:
    df = pd.DataFrame({"TASK_NAME": ["A", "B", "C", "B"],
                       "TASK_STATUS": ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "failed"],
                       "RUNTIME_SEC": [100, 5, 50, 3]})
    assert evidence_task_options(df, etl.FAILED_TASK_STATUSES) == ["B", "A", "C"]
    labels = evidence_task_labels(df, etl.FAILED_TASK_STATUSES)
    # review F12/F20: no runtime in the label (it was '... · 5.0s · ...'): a live runtime re-labels the
    # option every cache refresh, and on streamlit 1.52 a re-labelled selectbox snaps back to index 0
    assert labels["B"] == "B · failed · 2 attempts"
    assert labels["A"] == "A"


def test_task_labels_are_stable_while_a_task_runs() -> None:
    # a still-running task's RUNTIME_SEC grows between 5-minute refreshes: its label must not move
    before = pd.DataFrame({"TASK_NAME": ["A", "B"], "TASK_STATUS": ["RUNNING", "FAILED"],
                           "RUNTIME_SEC": [100, 5]})
    later = before.assign(RUNTIME_SEC=[4000, 5])
    assert evidence_task_labels(before, etl.FAILED_TASK_STATUSES) == \
        evidence_task_labels(later, etl.FAILED_TASK_STATUSES) == {"B": "B · failed", "A": "A"}


def test_evidence_run_id_is_the_run_the_table_shows() -> None:
    from app.logic.etl_evidence import evidence_run_id
    assert evidence_run_id(pd.DataFrame({"TASK_NAME": ["A", "B"], "RUN_ID": ["R-9", "R-9"]})) == "R-9"
    assert evidence_run_id(pd.DataFrame({"TASK_NAME": ["A"], "RUN_ID": [" 123 "]})) == "123"
    # no column / NULL / empty frame -> "" so the drill falls back to the latest-run predicates
    for df in (pd.DataFrame({"TASK_NAME": ["A"]}), pd.DataFrame({"TASK_NAME": ["A"], "RUN_ID": [None]}),
               pd.DataFrame({"RUN_ID": []}), None):
        assert evidence_run_id(df) == ""


def test_task_options_tolerate_missing_columns() -> None:
    assert evidence_task_options(pd.DataFrame({"TASK_NAME": ["A", "B", "A"]}), {"FAILED"}) == ["A", "B"]
    only_status = pd.DataFrame({"TASK_NAME": ["A", "B"], "TASK_STATUS": ["OK", "ABORTED"]})
    assert evidence_task_options(only_status, etl.FAILED_TASK_STATUSES) == ["B", "A"]
    assert evidence_task_options(pd.DataFrame({"X": [1]}), {"FAILED"}) == []
    assert evidence_task_options(None, {"FAILED"}) == []
    assert evidence_task_options(pd.DataFrame({"TASK_NAME": [None, float("nan"), " "]}), {"FAILED"}) == []
    assert evidence_task_labels(None, {"FAILED"}) == {}


# --- one test per verdict branch --------------------------------------------------------------------

def test_verdict_not_found() -> None:
    lines = task_evidence_lines(pd.DataFrame(), task="SP_X")
    assert _levels(lines) == ["no_data_yet"] and "no CONTROL_STATUS rows" in lines[0].text
    assert task_evidence_lines(None, task="SP_X")[0].level == "no_data_yet"


def test_verdict_no_call_recent_says_lag() -> None:
    lines = _lines(_row(CALL_QUERY_ID=None, MATCHED_CALLS=0, END_AGE_MIN=10))
    assert _levels(lines) == ["no_data_yet"]
    assert "~45 min" in lines[0].text and "10 min ago" in lines[0].text
    assert INFORMATICA_SIDE_SENTENCE not in lines[0].text


def test_verdict_no_call_points_at_the_tag_ask() -> None:
    lines = _lines(_row(CALL_QUERY_ID=None, MATCHED_CALLS=0, IS_TASK_FAILED=1), task="M_LOAD_X")
    assert _levels(lines) == ["no_data_yet"]
    assert lines[0].text == "No Snowflake CALL of M_LOAD_X ran in its window."
    assert "INFORMATICA_QUERY_TAG_ASK.md" in lines[0].hint and "M_*" in lines[0].hint
    # v4.603: hedged, not a blanket claim -- an M_* task CAN be a CALLed procedure (owner probe P3,
    # 2026-09-29: an M_* CONTROL_STATUS task CALLs a procedure of its own name), so the hint says a task that
    # sends its SQL directly is "typically" a mapping, never that mapping tasks never CALL.
    assert lines[0].hint.startswith("A task that sends its SQL directly instead of as a CALL (typically an "
                                    "M_* mapping), or whose name differs from its procedure, can't be matched")
    assert "Mapping (M_*) tasks send their SQL directly" not in lines[0].hint


def test_verdict_failed_call_leads_with_error() -> None:
    lines = _lines(_row(IS_CALL_FAILED=1, FAILED_CALLS=1, EXECUTION_STATUS="FAIL", ERROR_CODE=100132.0,
                        ERROR_MESSAGE="JavaScript execution error: Numeric value '$12' is not recognized",
                        IS_TASK_FAILED=1))
    assert lines[0].level == "error"
    text = lines[0].text
    assert text.startswith("Failed: JavaScript execution error")
    assert "(error 100132)" in text and "on WH_ETL at Sep 27, 22:01" in text
    # review F11: the Failed line names the CALL's database (same-named procs exist in every environment)
    assert "the Snowflake CALL of SP_X in ALFA_EDW_PRD on WH_ETL at Sep 27, 22:01" in text
    assert INFORMATICA_SIDE_SENTENCE not in " ".join(line.text for line in lines)
    padded = _lines(_row(IS_CALL_FAILED=1, FAILED_CALLS=1, ERROR_CODE="002043", ERROR_MESSAGE="x"))[0].text
    assert "(error 002043)" in padded                                     # a text code stays verbatim


def test_verdict_failed_call_falls_back_to_the_child_error_text() -> None:
    lines = _lines(_row(IS_CALL_FAILED=1, FAILED_CALLS=1, ERROR_MESSAGE=None,
                        CHILD_ERROR_MESSAGE="Duplicate row detected during DML action"))
    assert lines[0].level == "error" and "Duplicate row detected" in lines[0].text
    long = _lines(_row(IS_CALL_FAILED=1, FAILED_CALLS=1, ERROR_MESSAGE="x" * 900))[0].text
    assert "x" * 300 in long and "x" * 301 not in long                     # headline capped at 300


def test_verdict_failed_then_retried_is_a_warning() -> None:
    newest = _row(CALL_QUERY_ID="02-ok", MATCHED_CALLS=2, FAILED_CALLS=1)
    older = _row(CALL_QUERY_ID="01-bad", IS_CALL_FAILED=1, MATCHED_CALLS=2, FAILED_CALLS=1,
                 ERROR_MESSAGE="Warehouse suspended", CALL_START_TIME=pd.Timestamp("2026-09-27 22:00"))
    lines = _lines(newest, older)
    assert lines[0].level == "warn"
    assert lines[0].text.startswith("Failed then retried: 1 of 2 CALL attempt(s) of SP_X failed")
    assert "Warehouse suspended" in lines[0].text and "latest attempt succeeded" in lines[0].text
    assert "error" not in _levels(lines)


def test_verdict_uses_the_uncapped_failed_count() -> None:
    # FAILED_CALLS is the SQL window total — a failure beyond the row cap still counts
    lines = _lines(_row(MATCHED_CALLS=60, FAILED_CALLS=3))
    assert lines[0].level == "warn" and "3 of 60 CALL attempt(s)" in lines[0].text


def test_verdict_child_failed_inside_successful_call() -> None:
    lines = _lines(_row(FAILED_CHILD_STATEMENTS=2, CHILD_ERROR_MESSAGE="NULL result in a non-nullable column",
                        IS_TASK_FAILED=1))
    assert lines[0].level == "warn"
    assert lines[0].text.startswith("The CALL returned success but 2 statement(s) inside it failed")
    assert "NULL result" in lines[0].text and "return value" in lines[0].text
    assert INFORMATICA_SIDE_SENTENCE not in " ".join(line.text for line in lines)


def test_verdict_informatica_side_exact_sentence() -> None:
    lines = _lines(_row(IS_TASK_FAILED=1, TASK_STATUS="FAILED", END_AGE_MIN=45))
    assert lines[0].level == "warn"
    assert lines[0].text == ("No failed Snowflake CALL found in the window — the failure was likely on the "
                             "Informatica side (the Snowflake statements it ran succeeded).")
    assert "clean" not in _levels(lines)                 # a failed task is never called healthy


def test_verdict_informatica_side_hedged_when_recent() -> None:
    lines = _lines(_row(IS_TASK_FAILED=1, END_AGE_MIN=44))
    assert lines[0].level == "no_data_yet"
    assert "~45 min" in lines[0].text and "44 min ago" in lines[0].text
    assert INFORMATICA_SIDE_SENTENCE not in " ".join(line.text for line in lines)
    assert "clean" not in _levels(lines)


def test_verdict_informatica_side_needs_every_call_seen() -> None:
    # more CALLs matched than the capped rows show -> a hidden one could hold a failed statement
    lines = _lines(_row(IS_TASK_FAILED=1, MATCHED_CALLS=80))
    assert lines[0].level == "no_data_yet" and "80 CALLs" in lines[0].text
    assert INFORMATICA_SIDE_SENTENCE not in " ".join(line.text for line in lines)


def test_verdict_informatica_side_needs_a_matched_call() -> None:
    lines = _lines(_row(CALL_QUERY_ID=None, MATCHED_CALLS=0, IS_TASK_FAILED=1, END_AGE_MIN=600))
    assert INFORMATICA_SIDE_SENTENCE not in " ".join(line.text for line in lines)


@pytest.mark.parametrize("children", [None, 0, float("nan")])
def test_verdict_informatica_side_needs_the_statements_seen(children) -> None:
    """Review F14/F19: 'the Snowflake statements it ran succeeded' is only true of statements SEEN. With
    none linked to the CALL, a failed MERGE the procedure caught (branch 4's case) looks the same, so the
    drill hedges instead — and says it once (no separate 'No statements were found' line)."""
    lines = _lines(_row(IS_TASK_FAILED=1, TASK_STATUS="FAILED", CHILD_STATEMENTS=children, CHILD_ELAPSED_MS=None,
                        QUEUED_PCT=None))
    text = " ".join(line.text for line in lines)
    assert INFORMATICA_SIDE_SENTENCE not in text and "statements it ran succeeded" not in text
    assert lines[0].level == "no_data_yet"
    assert lines[0].text == ("The CALL of SP_X succeeded, but none of its statements were visible in "
                             "QUERY_HISTORY, so a failure the procedure caught can't be ruled out; the task's "
                             "failure may be on the Informatica side.")
    assert "No statements were found inside the CALL's session" not in text
    assert "clean" not in _levels(lines)


def test_verdict_informatica_side_needs_every_calls_statements() -> None:
    # two successful CALLs, only the newest linked its statements: the older one could hide a caught failure
    newest = _row(CALL_QUERY_ID="02", MATCHED_CALLS=2, IS_TASK_FAILED=1)
    older = _row(CALL_QUERY_ID="01", MATCHED_CALLS=2, IS_TASK_FAILED=1, CHILD_STATEMENTS=0)
    lines = _lines(newest, older)
    assert INFORMATICA_SIDE_SENTENCE not in " ".join(line.text for line in lines)
    assert lines[0].level == "no_data_yet"
    assert lines[0].text.startswith("Every CALL of SP_X succeeded, but 1 of the 2 showed no statements in "
                                    "QUERY_HISTORY, so a failure the procedure caught can't be ruled out")
    # every CALL's statements seen -> the owner's exact sentence
    both = _lines(newest, _row(CALL_QUERY_ID="01", MATCHED_CALLS=2, IS_TASK_FAILED=1))
    assert both[0].text == INFORMATICA_SIDE_SENTENCE


# --- review F15: a retried task CONTROL_STATUS still failed -------------------------------------------

def _retried(**newest_over):
    newest = _row(CALL_QUERY_ID="02-ok", MATCHED_CALLS=2, FAILED_CALLS=1, IS_TASK_FAILED=1, TASK_STATUS="FAILED",
                  **newest_over)
    older = _row(CALL_QUERY_ID="01-bad", IS_CALL_FAILED=1, MATCHED_CALLS=2, FAILED_CALLS=1, IS_TASK_FAILED=1,
                 TASK_STATUS="FAILED", EXECUTION_STATUS="FAIL", ERROR_MESSAGE="boom",
                 CALL_START_TIME=pd.Timestamp("2026-09-27 22:00"))
    return newest, older


def test_verdict_retried_but_still_failed_points_past_snowflake() -> None:
    lines = _lines(*_retried())
    assert lines[0].level == "warn" and lines[0].text.startswith("Failed then retried: 1 of 2")
    assert lines[1] == ev.EvidenceLine(
        "warn", "The latest CALL succeeded, yet CONTROL_STATUS failed the task — the final failure was likely "
                "on the Informatica side.")
    # never the owner's sentence: 'No failed Snowflake CALL found' is false when one attempt failed
    assert INFORMATICA_SIDE_SENTENCE not in " ".join(line.text for line in lines)


def test_verdict_retried_latest_call_caught_a_statement_failure() -> None:
    lines = _lines(*_retried(FAILED_CHILD_STATEMENTS=2, CHILD_ERROR_MESSAGE="MERGE dup row"))
    assert lines[0].text.startswith("Failed then retried:")
    assert lines[1].level == "warn"
    assert lines[1].text.startswith("The latest CALL returned success but 2 statement(s) inside it failed "
                                    "(MERGE dup row)")
    assert "Informatica side" not in lines[1].text
    # a caught failure is named even when the retry left the task SUCCEEDED
    ok_task = _lines(*[{**r, "IS_TASK_FAILED": 0, "TASK_STATUS": "SUCCEEDED"}
                       for r in _retried(FAILED_CHILD_STATEMENTS=1, CHILD_ERROR_MESSAGE="x")])
    assert ok_task[1].text.startswith("The latest CALL returned success but 1 statement(s)")


def test_verdict_retried_inside_the_lag_is_hedged() -> None:
    newest, older = _retried(END_AGE_MIN=20)
    lines = _lines(newest, {**older, "END_AGE_MIN": 20})
    assert lines[1].level == "no_data_yet" and "~45 min" in lines[1].text and "20 min ago" in lines[1].text
    assert "likely on the Informatica side" not in " ".join(line.text for line in lines)


def test_verdict_retried_blind_latest_call_is_hedged_once() -> None:
    lines = _lines(*_retried(CHILD_STATEMENTS=0, CHILD_ELAPSED_MS=None, QUEUED_PCT=None))
    assert lines[1].level == "no_data_yet"
    assert lines[1].text.startswith("The latest CALL succeeded, but none of its statements were visible")
    assert "No statements were found inside the CALL's session" not in " ".join(line.text for line in lines)


def test_verdict_retried_needs_every_call_listed() -> None:
    newest, older = _retried()
    lines = _lines({**newest, "MATCHED_CALLS": 70}, {**older, "MATCHED_CALLS": 70})
    assert lines[1].level == "no_data_yet" and "70 CALLs" in lines[1].text
    assert "likely on the Informatica side" not in " ".join(line.text for line in lines)


def test_verdict_retried_task_that_recovered_adds_nothing() -> None:
    newest, older = _retried()
    lines = _lines({**newest, "IS_TASK_FAILED": 0}, {**older, "IS_TASK_FAILED": 0})
    assert _levels(lines) == ["warn"] and lines[0].text.startswith("Failed then retried:")


# --- review F11: the CALL's database ------------------------------------------------------------------

def test_verdict_warns_when_the_call_ran_in_another_database() -> None:
    lines = _lines(_row(CALL_DATABASE="ALFA_EDW_SIT", CONTROL_DATABASE="ALFA_EDW_PRD"))
    warn = [line for line in lines if "CONTROL_STATUS's database" in line.text]
    assert warn and warn[0].level == "warn"
    assert warn[0].text == ("No CALL of SP_X ran in ALFA_EDW_PRD (CONTROL_STATUS's database) in this window; "
                            "the CALL(s) shown match by procedure name in ALFA_EDW_SIT — check that is this "
                            "environment's run before acting on them.")
    assert "clean" not in _levels(lines)            # another environment's CALL is never 'healthy' for this one
    # review r2: after the lag the database warning LEADS and another database's failure is never an error
    failed = _lines(_row(CALL_DATABASE="ALFA_EDW_SIT", IS_CALL_FAILED=1, FAILED_CALLS=1, ERROR_MESSAGE="boom"))
    assert failed[0].level == "warn" and failed[0].text.startswith("No CALL of SP_X ran in ALFA_EDW_PRD")
    assert failed[1].level == "warn" and "CALL of SP_X in ALFA_EDW_SIT on WH_ETL" in failed[1].text
    assert "error" not in _levels(failed)


def test_verdict_waits_out_the_lag_before_citing_another_databases_call() -> None:
    """Review r2: a running task (or one that ended < QH_LAG_MIN ago) whose own CALL has not landed must not be
    judged by a same-named CALL from another database -- one hedge, no 'Failed:' headline."""
    for age in (0, 10, 44):
        lines = _lines(_row(CALL_DATABASE="ALFA_EDW_SIT", IS_CALL_FAILED=1, FAILED_CALLS=1,
                            ERROR_MESSAGE="boom", END_AGE_MIN=age))
        assert _levels(lines) == ["no_data_yet"], age
        assert lines[0].text.startswith("No CALL of SP_X in ALFA_EDW_PRD (CONTROL_STATUS's database) in "
                                        "QUERY_HISTORY yet")
        assert "ALFA_EDW_SIT, likely another environment" in lines[0].text
    # the task's OWN database CALL inside the lag is still reported as it is
    own = _lines(_row(IS_CALL_FAILED=1, FAILED_CALLS=1, ERROR_MESSAGE="boom", END_AGE_MIN=10))
    assert own[0].level == "error" and own[0].text.startswith("Failed: boom")


def test_verdict_database_match_is_case_insensitive_and_needs_a_known_control_db() -> None:
    assert _levels(_lines(_row(CALL_DATABASE="alfa_edw_prd"))) == ["clean"]
    # a 1-/2-part CONTROL_STATUS FQN states no database: no preference, no warning
    assert _levels(_lines(_row(CALL_DATABASE="ALFA_EDW_SIT", CONTROL_DATABASE=""))) == ["clean"]
    # a frame without the columns (an older shape) is not second-guessed
    bare = {k: v for k, v in _row().items() if k not in ("CALL_DATABASE", "CONTROL_DATABASE")}
    assert _levels(_lines(bare)) == ["clean"]
    # review r3: an unresolved database is NOT another environment -- a note, and the verdicts still run
    unknown = _lines(_row(CALL_DATABASE=None))
    assert any("ran with no resolvable database" in line.text for line in unknown)
    assert not any("another environment" in line.text or "CONTROL_STATUS's database" in line.text for line in unknown)


def test_verdict_an_unresolved_database_never_hides_the_failure() -> None:
    """Review r3: 'CALL SP_X()' in a session with no current database fails to resolve (CALL_DATABASE NULL). That
    is not another environment: the failure stays the headline error, inside the lag and after it."""
    for age in (0, 10, 600):
        lines = _lines(_row(CALL_DATABASE=None, IS_CALL_FAILED=1, FAILED_CALLS=1, END_AGE_MIN=age,
                            ERROR_MESSAGE="Cannot perform CALL. This session does not have a current database."))
        assert lines[0].level == "warn" and "ran with no resolvable database" in lines[0].text, age
        assert lines[1].level == "error" and lines[1].text.startswith("Failed: Cannot perform CALL"), age
        assert not any("likely another environment" in line.text for line in lines), age


def test_verdict_queue_provisioning_vs_overload_wording() -> None:
    prov = _lines(_row(QUEUED_PCT=41.0, QUEUED_PROVISIONING_MS=900_000, QUEUED_OVERLOAD_MS=100))
    assert prov[0].level == "warn" and "41% of its statements' time was queued on WH_ETL" in prov[0].text
    assert "resume (provisioning)" in prov[0].text and "sizing up buys nothing" in prov[0].text
    over = _lines(_row(QUEUED_PCT=25.0, QUEUED_OVERLOAD_MS=900_000, QUEUED_PROVISIONING_MS=0,
                       QUEUE_WAREHOUSE="WH_BUSY"))
    assert "queued on WH_BUSY" in over[0].text and "behind other work (overload)" in over[0].text
    assert _levels(_lines(_row(QUEUED_PCT=19.9)))[0] == "clean"
    # v4.604.0 review r1 R1-11: the overload advice no longer says "add a cluster" ungated -- on a multi-cluster
    # warehouse a higher MAX_CLUSTER_COUNT helps only if its queries reach the current maximum (Cost checks it)
    assert over[0].text.endswith(
        "mostly waiting behind other work (overload) — move this step off the busy window, or add a cluster (on "
        "a multi-cluster warehouse, raise MAX_CLUSTER_COUNT only if its queries reach the current maximum — Cost "
        "Intelligence ▸ Optimization & Savings ▸ Idle & sizing ▸ Check cluster use checks it).")
    assert "add a cluster or move this step off the busy window" not in over[0].text


def test_verdict_remote_spill_humanized() -> None:
    lines = _lines(_row(SPILL_REMOTE_GB=12.5))
    assert lines[0].level == "warn" and "12.5 GB to remote storage" in lines[0].text
    local = _lines(_row(SPILL_LOCAL_GB=2.0))
    assert "2.0 GB to local disk" in local[0].text
    assert _levels(_lines(_row(SPILL_LOCAL_GB=0.5)))[0] == "clean"          # small local spill is noise


def test_verdict_compile_heavy() -> None:
    lines = _lines(_row(COMPILE_MS=1_200_000, CHILD_ELAPSED_MS=3_400_000))
    assert lines[0].level == "warn" and "compilation took 35%" in lines[0].text and "20m" in lines[0].text


def test_verdict_outside_snowflake_uses_window_totals() -> None:
    # two CALL rows of 3.5M ms each would sum past the window; only the SQL window total counts
    rows = [_row(CALL_QUERY_ID=q, ALL_CALLS_ELAPSED_MS=600_000, CALL_ELAPSED_MS=3_500_000) for q in ("a", "b")]
    lines = _lines(*rows)
    outside = [line for line in lines if line.text.startswith("Most of the task's time was outside Snowflake")]
    assert outside and "ran 10m of the task's 1h window (17%)" in outside[0].text
    assert not [line for line in _lines(_row(TASK_WINDOW_SEC=59, ALL_CALLS_ELAPSED_MS=1_000))
                if "outside Snowflake" in line.text]                     # too short a window to judge


@pytest.mark.parametrize("age", [0, 10, 44])
def test_verdict_outside_snowflake_is_lag_gated(age) -> None:
    """Review F13: inside QUERY_HISTORY's lag the rest of the window may be a retry still running IN
    Snowflake (a running task has END_AGE_MIN 0 and a window growing to now) or a CALL not landed yet —
    never attribute it to Informatica there."""
    retrying = [_row(CALL_QUERY_ID="01-bad", IS_CALL_FAILED=1, FAILED_CALLS=1, ERROR_MESSAGE="boom",
                     TASK_WINDOW_SEC=5400, ALL_CALLS_ELAPSED_MS=480_000, END_AGE_MIN=age, IS_TASK_FAILED=0)]
    text = " ".join(line.text for line in _lines(*retrying))
    assert "outside Snowflake" not in text and "Informatica-side" not in text
    # the same row once the lag has passed does name it
    later = _lines({**retrying[0], "END_AGE_MIN": 45})
    assert any(line.text.startswith("Most of the task's time was outside Snowflake") for line in later)


def test_verdict_no_children_says_so() -> None:
    lines = _lines(_row(CHILD_STATEMENTS=None, QUEUED_PCT=None, CHILD_ELAPSED_MS=None))
    assert _levels(lines) == ["clean", "no_data_yet"]
    assert lines[0].text == "Snowflake side looks healthy for SP_X: its CALL succeeded."
    assert "No statements were found inside the CALL's session" in lines[1].text


def test_verdict_clean_when_healthy() -> None:
    lines = _lines(_row())
    assert _levels(lines) == ["clean"]
    assert lines[0].text.startswith("Snowflake side looks healthy for SP_X: the CALL succeeded")


def test_verdict_null_renders_em_dash() -> None:
    lines = _lines(_row(IS_CALL_FAILED=1, FAILED_CALLS=1, WAREHOUSE_NAME=None, ERROR_CODE=None,
                        ERROR_MESSAGE="boom", CALL_START_TIME=pd.NaT))
    assert "(error —)" in lines[0].text and "on — at —" in lines[0].text
    assert "None" not in lines[0].text and "nan" not in lines[0].text and "NaT" not in lines[0].text


# --- the display frame + the column contract --------------------------------------------------------

def test_display_frame_drops_helpers_and_empty_call_rows() -> None:
    df = pd.DataFrame([_row(), _row(CALL_QUERY_ID=None), _row(CALL_QUERY_ID=float("nan"))])
    disp = evidence_display_frame(df)
    assert len(disp) == 1
    cols = list(disp.columns)
    assert cols[0] == "CALL_START_TIME" and cols[-1] == "CALL_QUERY_ID"
    # review F11: which environment each CALL was is on the row, next to its warehouse
    assert cols.index("CALL_DATABASE") + 1 == cols.index("WAREHOUSE_NAME")
    for helper in ("TASK_START_TIME", "TASK_END_TIME", "ATTEMPTS", "TASK_STATUS", "IS_TASK_FAILED",
                   "IS_CALL_FAILED", "TASK_WINDOW_SEC", "END_AGE_MIN", "MATCHED_CALLS", "FAILED_CALLS",
                   "ALL_CALLS_ELAPSED_MS", "CHILD_ELAPSED_MS", "CONTROL_DATABASE"):
        assert helper not in disp.columns, helper
    assert evidence_display_frame(None).empty and evidence_display_frame(pd.DataFrame()).empty
    assert evidence_display_frame(pd.DataFrame([_row(CALL_QUERY_ID=None)])).empty


def test_builder_and_verdict_share_a_column_contract() -> None:
    from test_pages_shaped import _shaped_from_sql

    sql = etl.run_task_evidence_scan("ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS", task="SP_X", workflow="WF")
    shaped = _shaped_from_sql(sql).df
    assert not shaped.empty, "the builder SQL no longer parses into a shaped frame"
    # the shaped harness fills text columns with placeholders; make the CALL this environment's (review r2:
    # another database's CALL is never an error), so the contract below still reads the failed-CALL branch
    shaped = shaped.assign(CALL_DATABASE=shaped["CONTROL_DATABASE"])
    lines = task_evidence_lines(shaped, task="SP_X")
    assert lines[0].level == "error"                      # the shaped row 0 is a failed CALL
    disp = evidence_display_frame(shaped)
    assert list(disp.columns) == [c for c in ev._DISPLAY_COLUMNS if c in shaped.columns]
    assert set(ev._DISPLAY_COLUMNS) <= set(shaped.columns)   # every displayed column is selected
