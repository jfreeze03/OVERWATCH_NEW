"""v4.606 holistic review: every ETL panel names the Window it read, one rule names it, and a creep
panel with nothing fitted is not a green all-clear.

On the 1st of a month under Current month the ETL readers read TODAY (R1-063: a calendar day-0
offset is today, not all time). Only the runtimes and recon-recurrence panels said so; Failure
recurrence and Runtime creep carried no window, and creep -- whose fit needs CREEP_MIN_RUNS runs per
task -- rendered the green 'fitted trends are flat' row over one run per task. And the recon
recurrence label called a since-the-1st read 'in the last N days' while the runtimes label said
'since <first day>' for the same read.

The panels run against recording fakes (the tests/test_ops_c01_p606.py pattern; AppTest is skipped
on the floor CI leg, so these run everywhere).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.logic.date_windows import CalendarDayOffset
from app.logic.insights import CREEP_MIN_RUNS

_OCT1 = date(2026, 10, 1)


class _St:
    """The slice of streamlit the three ETL panels touch, recording warnings + captions."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.captions: list[str] = []

    def warning(self, text, *_a, **_k) -> None:
        self.warnings.append(str(text))

    def caption(self, text, *_a, **_k) -> None:
        self.captions.append(str(text))


def _render(monkeypatch, panel: str, days: object, frame: pd.DataFrame, *, today: date = _OCT1,
            settings_key: str = "ETL_CONTROL_STATUS_FQN"):
    """Run one operations ETL panel against a stubbed read; return (absence states, fake st).

    ``guard`` and ``empty_state`` both record (kind, message): guard records only its empty branch,
    the same split the real guard routes through components.empty_state."""
    import app.ui.pages.operations as ops

    seen: list[tuple[str, str]] = []
    fake = _St()

    def fake_guard(res, msg, *_a, kind: str = "no_data_yet", **_k) -> bool:
        if res.ok and res.empty:
            seen.append((kind, msg))
        return res.ok and not res.empty

    monkeypatch.setattr(ops, "st", fake)
    monkeypatch.setattr(ops, "account_today", lambda: today)
    monkeypatch.setattr(ops, "run", lambda *_a, **_k: QueryResult(df=frame, ok=True, source="t"))
    monkeypatch.setattr(ops, "load_settings", lambda _p: {settings_key: "DB.S.T"})
    monkeypatch.setattr(ops, "guard", fake_guard)
    monkeypatch.setattr(ops, "empty_state", lambda kind, msg, *_a, **_k: seen.append((kind, msg)))
    monkeypatch.setattr(ops, "_etl_proc_changes", lambda *_a, **_k: None)
    for name in ("section_header", "styled_table", "result_caption"):
        monkeypatch.setattr(ops, name, lambda *_a, **_k: None)
    getattr(ops, panel)(days)
    return seen, fake


def _runtime_series(runs_per_task: int, *, tasks: int = 2, sec: float = 300.0) -> pd.DataFrame:
    """task_runtime_history_scan rows: ``tasks`` flat series of ``runs_per_task`` runs each."""
    rows = [{"WORKFLOW_NAME": "WF_NIGHTLY", "TASK_NAME": f"SP_LOAD_{t}", "RN": rn, "RUNTIME_SEC": sec}
            for t in range(tasks) for rn in range(1, runs_per_task + 1)]
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------ Runtime creep ----

def test_creep_with_nothing_fitted_is_no_data_yet_not_a_green_all_clear(monkeypatch):
    # Current month on Oct 1: each nightly task has ONE run in today's window, so nothing is fitted.
    # It used to render empty_state("clean", "...the fitted trends are flat or improving...").
    seen, _ = _render(monkeypatch, "_runtime_creep_panel", CalendarDayOffset(0), _runtime_series(1))
    assert seen == [("no_data_yet", f"Not enough runs (today) to fit a trend — each task needs at least "
                                    f"{CREEP_MIN_RUNS}; widen the scope-bar Window.")]


def test_creep_too_few_runs_mid_month_names_the_first_day(monkeypatch):
    seen, _ = _render(monkeypatch, "_runtime_creep_panel", CalendarDayOffset(2),
                      _runtime_series(CREEP_MIN_RUNS - 1), today=date(2026, 10, 3))
    assert seen == [("no_data_yet", f"Not enough runs (since Oct 1) to fit a trend — each task needs at "
                                    f"least {CREEP_MIN_RUNS}; widen the scope-bar Window.")]


def test_creep_unscoped_too_few_runs_does_not_say_widen_the_window(monkeypatch):
    # a plain 0 reads every task's last runs (no Window clause): widening the Window cannot help
    seen, _ = _render(monkeypatch, "_runtime_creep_panel", 0, _runtime_series(2))
    assert seen == [("no_data_yet", f"Not enough runs to fit a trend — each task needs at least "
                                    f"{CREEP_MIN_RUNS}.")]


def test_creep_clean_only_when_a_series_was_fitted_and_it_names_the_window(monkeypatch):
    frame = pd.concat([_runtime_series(CREEP_MIN_RUNS, tasks=1),
                       _runtime_series(1, tasks=1).assign(TASK_NAME="SP_WEEKLY")], ignore_index=True)
    seen, fake = _render(monkeypatch, "_runtime_creep_panel", 30, frame)
    assert seen == [("clean", "No task is trending materially slower run-over-run (last 30d) — the fitted "
                              f"trends are flat or improving. (1 of 2 task(s) had the {CREEP_MIN_RUNS}+ "
                              "runs a trend needs.)")]
    assert fake.warnings == []


def test_creep_warning_and_empty_read_name_the_window(monkeypatch):
    creeping = pd.DataFrame({"WORKFLOW_NAME": "WF", "TASK_NAME": "SP_SLOW", "RN": [5, 4, 3, 2, 1],
                             "RUNTIME_SEC": [100.0, 200.0, 300.0, 400.0, 500.0]})
    seen, fake = _render(monkeypatch, "_runtime_creep_panel", CalendarDayOffset(9), creeping,
                         today=date(2026, 9, 10))
    assert seen == []
    assert len(fake.warnings) == 1
    assert "1 task(s) are trending slower run-over-run (since Sep 1). Steepest: **SP_SLOW**" in fake.warnings[0]
    seen, _ = _render(monkeypatch, "_runtime_creep_panel", CalendarDayOffset(0), pd.DataFrame())
    assert seen == [("no_data_yet", "No runtime history (today) to fit a trend yet.")]


def test_creep_fit_coverage_counts_the_runs_the_fit_uses():
    from app.logic.insights import creep_fit_coverage
    assert creep_fit_coverage(None) == (0, 0)
    assert creep_fit_coverage(pd.DataFrame()) == (0, 0)
    assert creep_fit_coverage(pd.DataFrame({"WORKFLOW_NAME": ["W"]})) == (0, 0)
    assert creep_fit_coverage(_runtime_series(1, tasks=3)) == (0, 3)
    assert creep_fit_coverage(_runtime_series(CREEP_MIN_RUNS, tasks=2)) == (2, 2)
    # a non-numeric runtime is not a usable run (etl_runtime_creep drops it too)
    short = _runtime_series(CREEP_MIN_RUNS, tasks=1).astype({"RUNTIME_SEC": object})
    short.loc[0, "RUNTIME_SEC"] = "n/a"
    assert creep_fit_coverage(short) == (0, 1)


# -------------------------------------------------------------------- Failure recurrence ----

def _status_rows(failed: list[int]) -> pd.DataFrame:
    return pd.DataFrame({"WORKFLOW_NAME": "WF", "TASK_NAME": "SP_LOAD", "RN": range(1, len(failed) + 1),
                         "IS_FAILED": failed, "IS_RUNNING": 0})


@pytest.mark.parametrize(("days", "today", "scope"), [
    (CalendarDayOffset(0), _OCT1, " (today)"),
    (CalendarDayOffset(9), date(2026, 9, 10), " (since Sep 1)"),
    (7, _OCT1, " (last 7d)"),
    (0, _OCT1, ""),
])
def test_failure_recurrence_names_the_window_on_every_outcome(monkeypatch, days, today, scope):
    seen, _ = _render(monkeypatch, "_failure_recurrence_panel", days, _status_rows([0]), today=today)
    assert seen == [("clean", f"No task has failed in the scoped runs{scope} — nothing is trending toward "
                              "a repeat failure. (A task needs a failure in the window to appear.)")]
    seen, fake = _render(monkeypatch, "_failure_recurrence_panel", days, _status_rows([1]), today=today)
    assert seen == []
    assert len(fake.warnings) == 1
    assert f"1 task(s) failing or at risk of failing again{scope} — " in fake.warnings[0]
    seen, _ = _render(monkeypatch, "_failure_recurrence_panel", days, pd.DataFrame(), today=today)
    assert seen == [("no_data_yet", f"No run history{scope} to judge recurrence yet.")]


# --------------------------------------------------------------- Reconciliation recurrence ----

def test_recon_recurrence_names_a_since_the_1st_read_like_the_runtimes_label(monkeypatch):
    # Current month on Sep 10: the scan reads LOAD_DTTM >= Sep 1 (account date). It used to say
    # 'No reconciliation errors in the last 9 days' while the runtimes header said '(since Sep 1)'.
    import app.ui.pages.operations as ops
    seen, _ = _render(monkeypatch, "_recon_recurrence_panel", CalendarDayOffset(9), pd.DataFrame(),
                      today=date(2026, 9, 10), settings_key="ETL_RECON_ERROR_FQN")
    assert seen == [("clean", "No reconciliation errors since Sep 1 — every metric ties out.")]
    assert ops._etl_window_suffix(CalendarDayOffset(9)) == " (since Sep 1)"   # the same words
    seen, _ = _render(monkeypatch, "_recon_recurrence_panel", CalendarDayOffset(0), pd.DataFrame(),
                      settings_key="ETL_RECON_ERROR_FQN")
    assert seen == [("clean", "No reconciliation errors today — every metric ties out.")]
    seen, _ = _render(monkeypatch, "_recon_recurrence_panel", 30, pd.DataFrame(),
                      settings_key="ETL_RECON_ERROR_FQN")
    assert seen == [("clean", "No reconciliation errors in the last 30 days — every metric ties out.")]


@pytest.mark.parametrize(("days", "today"), [
    (CalendarDayOffset(0), date(2026, 9, 1)),
    (CalendarDayOffset(9), date(2026, 9, 10)),
    (CalendarDayOffset(272), date(2026, 9, 30)),
])
def test_one_rule_names_every_calendar_window(monkeypatch, days, today):
    # the runtimes suffix and the recon phrase are the same words for the same calendar read
    from app.data import etl_control_sql
    from app.ui.pages import operations as ops
    monkeypatch.setattr(ops, "account_today", lambda: today)
    phrase = etl_control_sql.recon_window_phrase(days, today=today)
    assert ops._etl_window_suffix(days) == f" ({phrase})"
    assert phrase == etl_control_sql.calendar_window_phrase(days, today=today)


def test_the_panels_share_the_suffix_helper():
    # source lock: the two panels the review found unlabelled take the shared suffix, not a private one
    from tests._source import read
    src = read("app/ui/pages/operations.py")
    for fn in ("_failure_recurrence_panel", "_runtime_creep_panel"):
        start = src.index(f"def {fn}(")
        body = src[start:src.index("\ndef ", start + 1)]
        assert "_scope = _etl_window_suffix(days)" in body, fn
