"""Phase 4 — DST / account-time regression locks.

The account runs America/Chicago; under SiS the process clock is UTC. Between
Chicago evening and Chicago midnight the two disagree about what "today" is,
so any *business* date read from the server clock lands a day late for hours of
every day. formulas.account_today() is the one sanctioned reading of "today";
these locks pin its DST behavior and pin the rule that nothing in the pure
layers reads the clock directly.

The bug this file was written for: contract_planner.plan_scenarios computed
CURRENT_CONTRACT_EXHAUSTED from a bare date.today(). The pre-existing lock
called it with remaining_usd=0, which short-circuits to "n/a" and never
executed the date line — the function was covered, the bug was not.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from app.logic import actions, contract_planner, formulas

_ROOT = Path(__file__).resolve().parents[1]
_UTC = UTC


class _FrozenClock:
    """Stand-in for the datetime class with a fixed instant.

    account_today() resolves ``datetime`` from the formulas module globals, so
    patching formulas.datetime freezes it no matter which module imported it.
    """

    def __init__(self, utc_instant: datetime) -> None:
        self._instant = utc_instant

    def now(self, tz=None):
        return self._instant.astimezone(tz) if tz else self._instant


def _at(monkeypatch: pytest.MonkeyPatch, utc_iso: str) -> date:
    """The account's date at a given UTC instant."""
    instant = datetime.fromisoformat(utc_iso).replace(tzinfo=_UTC)
    monkeypatch.setattr(formulas, "datetime", _FrozenClock(instant))
    return formulas.account_today()


# ---------------------------------------------------------------------------
# 1. account_today() reads account time, not server time
# ---------------------------------------------------------------------------

def test_utc_evening_is_still_the_previous_account_day(monkeypatch):
    # 04:59 UTC == 23:59 Chicago the day BEFORE. The server clock says the
    # 15th; the account is still on the 14th. This is the whole bug.
    assert _at(monkeypatch, "2026-07-15 04:59") == date(2026, 7, 14)


def test_account_midnight_flips_the_day(monkeypatch):
    assert _at(monkeypatch, "2026-07-15 05:00") == date(2026, 7, 15)


def test_utc_midday_agrees_with_the_server(monkeypatch):
    # Most of the day the two agree — which is exactly why this bug hides.
    assert _at(monkeypatch, "2026-07-14 12:00") == date(2026, 7, 14)


# ---------------------------------------------------------------------------
# 2. The zone is a zone, not a fixed offset
# ---------------------------------------------------------------------------

def test_divergence_window_is_five_hours_in_summer(monkeypatch):
    # CDT is UTC-5, so account midnight lands at 05:00 UTC.
    assert _at(monkeypatch, "2026-07-15 04:59") == date(2026, 7, 14)
    assert _at(monkeypatch, "2026-07-15 05:00") == date(2026, 7, 15)


def test_divergence_window_is_six_hours_in_winter(monkeypatch):
    # CST is UTC-6, so account midnight lands an hour later, at 06:00 UTC.
    # A fixed -5 offset would call 05:59 UTC "the 15th" and be wrong.
    assert _at(monkeypatch, "2026-01-15 05:59") == date(2026, 1, 14)
    assert _at(monkeypatch, "2026-01-15 06:00") == date(2026, 1, 15)


def test_spring_forward_moves_the_boundary(monkeypatch):
    # 2026-03-08 is the spring-forward date. The SAME UTC wall time (05:30)
    # answers differently on either side of it: still-yesterday under CST,
    # already-today under CDT. No fixed offset satisfies both lines.
    assert _at(monkeypatch, "2026-03-08 05:30") == date(2026, 3, 7)   # CST
    assert _at(monkeypatch, "2026-03-09 05:30") == date(2026, 3, 9)   # CDT


def test_fall_back_moves_the_boundary_back(monkeypatch):
    # 2026-11-01 is the fall-back date; the boundary slides the other way.
    assert _at(monkeypatch, "2026-10-31 05:30") == date(2026, 10, 31)  # CDT
    assert _at(monkeypatch, "2026-11-02 05:30") == date(2026, 11, 1)   # CST


# ---------------------------------------------------------------------------
# 3. The bug itself: contract exhaustion is anchored in account time
# ---------------------------------------------------------------------------

def _exhaustion(rows: list[dict]) -> str:
    return next(r for r in rows if r["GROWTH"] == "+0%")["CURRENT_CONTRACT_EXHAUSTED"]


def test_exhaustion_anchors_to_account_day_not_server_day(monkeypatch):
    # $365 left at $1/day = 365 days. Frozen at 04:59 UTC on the 15th, where
    # the account is still on the 14th: exhaustion must date from the 14th.
    # Reading the server clock here would report a day late, every evening.
    monkeypatch.setattr(
        formulas, "datetime",
        _FrozenClock(datetime(2026, 7, 15, 4, 59, tzinfo=_UTC)))
    rows = contract_planner.plan_scenarios(1.0, 12, 0.0, remaining_usd=365.0)
    assert _exhaustion(rows) == "2027-07-14"


def test_exhaustion_accepts_an_injected_today():
    rows = contract_planner.plan_scenarios(
        1.0, 12, 0.0, remaining_usd=365.0, today=date(2026, 1, 1))
    assert _exhaustion(rows) == "2027-01-01"


def test_exhaustion_still_degrades_when_nothing_remains():
    # The path the old lock exercised — kept, so the fix didn't move it.
    rows = contract_planner.plan_scenarios(100.0, 12, 15.0, remaining_usd=0)
    assert _exhaustion(rows) == "n/a"


# ---------------------------------------------------------------------------
# 4. The drift guard: pure layers must not read the server clock
# ---------------------------------------------------------------------------

# The ONE sanctioned bare reading: account_today()'s own fallback for
# environments without tzdata. Everything else must go through account_today().
_CLOCK_ALLOWLIST = {
    "app/logic/formulas.py": "defines account_today(); its tzdata fallback is the source of truth",
}

def _pure_modules() -> list[Path]:
    return sorted([*(_ROOT / "app" / "logic").rglob("*.py"),
                   *(_ROOT / "app" / "data").rglob("*.py")])


def _is_server_clock_read(node: ast.AST) -> bool:
    """True for any ``X.today()`` and any naive ``X.now()``.

    Matched on the AST, not by regex: docstrings and comments discussing
    ``date.today()`` are prose, not calls, and must not trip the guard.

    Deliberately matched on the method name alone rather than on a ``date.``/
    ``datetime.`` base. canary.py really did spell it
    ``__import__("datetime").date.today()``, whose base is a Call, not a Name —
    a base-anchored check would wave through the one spelling this codebase has
    actually used. Nothing in these layers has a legitimate ``.today()``, and a
    tz-aware ``.now(tz=...)`` still passes, which is what account_today() does.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr == "today":
        return True
    return node.func.attr == "now" and not node.args and not node.keywords


def test_pure_layers_never_read_the_server_clock():
    """A business date in app/logic or app/data must come from account_today().

    These layers compute windows, boundaries and projections against
    ACCOUNT_USAGE, which stores account time. A bare date.today() here is a
    silent off-by-one for part of every day. Wall-clock stamps (cache
    fetched_at, export filenames) legitimately use the server clock and live
    in app/core and app/ui, which this guard deliberately does not cover.
    """
    offenders = []
    for path in _pure_modules():
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _CLOCK_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{rel}:{node.lineno}: {ast.unparse(node)}"
                      for node in ast.walk(tree) if _is_server_clock_read(node)]
    assert not offenders, (
        "server-clock reads in the pure layers; use formulas.account_today():\n"
        + "\n".join(offenders))


def test_the_allowlist_is_not_a_dumping_ground():
    # If this grows, the guard above stops meaning anything.
    assert len(_CLOCK_ALLOWLIST) == 1


def test_formulas_fallback_still_exists():
    # The allowlisted read is load-bearing: without tzdata, account_now() must
    # still return *a* time rather than raising.
    src = (_ROOT / "app" / "logic" / "formulas.py").read_text(encoding="utf-8")
    assert "except (ImportError, KeyError)" in src
    assert "return datetime.now()" in src


# ---------------------------------------------------------------------------
# 5. account_now() — the timestamp form, for comparisons against mart rows
# ---------------------------------------------------------------------------

def _account_now_at(monkeypatch, utc_iso: str) -> datetime:
    instant = datetime.fromisoformat(utc_iso).replace(tzinfo=_UTC)
    monkeypatch.setattr(formulas, "datetime", _FrozenClock(instant))
    return formulas.account_now()


def test_account_now_is_account_local_and_naive(monkeypatch):
    # 04:59 UTC is 23:59 the previous evening in Chicago (CDT, UTC-5).
    now = _account_now_at(monkeypatch, "2026-07-15 04:59")
    assert (now.year, now.month, now.day, now.hour, now.minute) == (2026, 7, 14, 23, 59)
    # Naive: the mart columns it is compared against carry no tzinfo, and
    # pandas refuses to compare tz-aware against tz-naive.
    assert now.tzinfo is None


def test_account_today_delegates_to_account_now(monkeypatch):
    monkeypatch.setattr(
        formulas, "datetime",
        _FrozenClock(datetime(2026, 7, 15, 4, 59, tzinfo=_UTC)))
    assert formulas.account_today() == formulas.account_now().date()


def test_overdue_is_judged_in_account_time_not_server_time(monkeypatch):
    """An action due later tonight (account time) is not overdue yet.

    Constructed so a server clock cannot pass it. Frozen account-now is
    23:00 on 6/14; 'not-yet' is due at 23:30, so only 'past-due' is overdue and
    it must rank first. A wall-clock now() is far past both due dates, marks
    BOTH overdue, and then the tiebreak falls to age — which 'not-yet' wins by
    two weeks, inverting the order. Same rows, opposite answer.
    """
    monkeypatch.setattr(
        formulas, "datetime",
        _FrozenClock(datetime(2026, 6, 15, 4, 0, tzinfo=_UTC)))  # 23:00 Chicago, 6/14
    df = pd.DataFrame({
        "ID": ["past-due", "not-yet"],
        "STATUS": ["OPEN", "OPEN"],
        "SEVERITY": ["HIGH", "HIGH"],
        "DUE_DATE": [datetime(2026, 6, 14, 22, 0),    # before account-now -> overdue
                     datetime(2026, 6, 14, 23, 30)],  # after  account-now -> not yet
        "CREATED_AT": [datetime(2026, 6, 14, 20, 0),  # 3 hours old
                       datetime(2026, 6, 1, 9, 0)],   # two weeks old
    })
    ranked = actions.rank_actions(df, limit=10)
    assert list(ranked["ID"]) == ["past-due", "not-yet"]
