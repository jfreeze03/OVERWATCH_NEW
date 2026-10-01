"""backfill_365.sql fills the security login fact as deep as the Security page's fact gates read
(v4.606 holistic review, read-cost lens).

v4.606 moved the new-network baseline BEFORE the window, so the panel's fact twin serves only when
FACT_SECURITY_LOGIN_DAILY is dense over the window plus 90 baseline days: 97 complete days at the
default 7-day window, 180 at a 90-day one. The backfill still called SP_LOAD_SECURITY_FACTS(90), so
right after a rebuild the gate failed at EVERY window and the panel fell back to its live
LOGIN_HISTORY scan of up to 180 days (beyond MAX_LIVE_WINDOW_DAYS). At a 90-day window it stayed
there until the hourly task had added 90 more days, and because the Access batch serves on the
'hourly' tier only when both login gates pass, its reads re-ran on the 300s tier meanwhile. The
backfill now loads 180 days, the loader's own maximum and its retention, so every window passes
from the first render after a rebuild.

The fact the backfill leaves is MODELLED from the loader's current definition (its DAYS_BACK clamp,
its LOGIN_HISTORY load predicate and its retention purge, each matched exactly once so a reshaped
loader fails here instead of being scored by a stale model). The gate is the page's real one:
capped_window + coverage_required_days + fact_coverage_complete over the coverage reader's own SQL,
evaluated by the c05 coverage simulator. Fails on b13e87f0 (SP_LOAD_SECURITY_FACTS(90)).

The prod fact (V075's own first fill was 90 days) reaches 180 days only as the hourly task adds them;
a one-time CALL SP_LOAD_SECURITY_FACTS(180) is the owner's catch-up, outside this script.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW, DAY_WINDOW_OPTIONS, LAST_MONTH_WINDOW
from app.data import security_sql
from app.logic.date_windows import window_bounds
from tests._source import ROOT, read
from tests.test_security_c05_fixes import _simulated_coverage

_PROC = "SP_LOAD_SECURITY_FACTS"
_BACKFILL = "snowflake/backfill_365.sql"
#: the caps pages/security.py serves the login readers (30) and the new-network reader (90) at
_LOGIN_CAP, _NETWORK_CAP = 30, 90

_TODAYS = (date(2026, 9, 30), date(2026, 10, 1), date(2026, 12, 31), date(2027, 1, 1), date(2027, 3, 1))
_CALENDAR = (CURRENT_MONTH_WINDOW, LAST_MONTH_WINDOW, CURRENT_YEAR_WINDOW)
#: (id, page days, calendar window or None, the account's today)
_WINDOWS = (
    [(f"{d}d", d, None, _TODAYS[0]) for d in DAY_WINDOW_OPTIONS]
    + [(f"{w.lower()}@{t.isoformat()}", 0, w, t) for w in _CALENDAR for t in _TODAYS]
)


def _current_definer() -> str:
    """The loader's CURRENT body: the last migration that CREATE OR REPLACEs it."""
    pat = re.compile(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\." + _PROC + r"\(")
    for mig in sorted((ROOT / "snowflake" / "migrations").glob("V[0-9]*.sql"), reverse=True):
        text = mig.read_text(encoding="utf-8")
        hits = list(pat.finditer(text))
        if hits:
            start = hits[-1].start()
            return text[start:text.index("\n$$;", start)]
    raise AssertionError(f"no migration defines {_PROC}")


def _one(pattern: str, text: str) -> re.Match[str]:
    hits = list(re.finditer(pattern, text))
    assert len(hits) == 1, f"{pattern!r} matched {len(hits)} times: the loader changed shape, re-model it"
    return hits[0]


def _loader() -> tuple[int, int]:
    """(DAYS_BACK maximum, FACT_SECURITY_LOGIN_DAILY retention days) from the current definer."""
    body = _current_definer()
    clamp = _one(r"d := GREATEST\(1, LEAST\(COALESCE\(DAYS_BACK, \d+\), (\d+)\)\)::INT;", body)
    purge = _one(r"DELETE FROM DBA_MAINT_DB\.OVERWATCH\.FACT_SECURITY_LOGIN_DAILY\s+"
                 r"WHERE DAY < DATEADD\('day', -(\d+), CURRENT_DATE\(\)\);", body)
    # the reload deletes and re-inserts DAY >= today - d from LOGIN_HISTORY (today's partition partial)
    _one(r"DELETE FROM DBA_MAINT_DB\.OVERWATCH\.FACT_SECURITY_LOGIN_DAILY\s+"
         r"WHERE DAY >= DATEADD\('day', -:d, CURRENT_DATE\(\)\);", body)
    _one(r"FROM SNOWFLAKE\.ACCOUNT_USAGE\.LOGIN_HISTORY\s+"
         r"WHERE EVENT_TIMESTAMP >= DATEADD\('day', -:d, CURRENT_DATE\(\)\)", body)
    return int(clamp.group(1)), int(purge.group(1))


def _backfill_days() -> int:
    calls = re.findall(r"^\s*CALL DBA_MAINT_DB\.OVERWATCH\." + _PROC + r"\((\d+)\);", read(_BACKFILL), re.M)
    assert len(calls) == 1, calls
    return int(calls[0])


def _fact_after_backfill(today: date, *, depth: int | None = None, days_later: int = 0) -> set[date]:
    """FACT_SECURITY_LOGIN_DAILY days ``days_later`` days after the backfill ran: the loader's clamped
    window, then one day per day from the hourly task, less what the retention purge drops."""
    maximum, keep = _loader()
    d = max(1, min(_backfill_days() if depth is None else depth, maximum))
    ran = today - timedelta(days=days_later)
    first = max(ran - timedelta(days=d), today - timedelta(days=keep))
    return {first + timedelta(days=n) for n in range((today - first).days + 1)}


def _gates(monkeypatch, today: date, days: int, window: str | None, fact: set[date]) -> tuple[bool, bool]:
    """(login gate, new-network gate) exactly as pages/security.py computes them."""
    from app.logic import security as logic
    monkeypatch.setattr(logic, "account_today", lambda: today)
    bounds = window_bounds(window, today) if window else None
    coverage = _simulated_coverage(lambda _today: fact)
    ld, lb = logic.capped_window(days, bounds, _LOGIN_CAP)
    nd, nb = logic.capped_window(days, bounds, _NETWORK_CAP)
    baseline = security_sql.NETWORK_BASELINE_DAYS
    login = logic.fact_coverage_complete(coverage(security_sql.security_login_fact_coverage(ld, bounds=lb)),
                                         logic.coverage_required_days(ld, lb))
    network = logic.fact_coverage_complete(
        coverage(security_sql.security_login_fact_coverage(nd, bounds=nb, lookback=baseline)),
        logic.coverage_required_days(nd, nb, lookback=baseline))
    return login, network


def test_the_model_reads_the_pages_own_caps_and_the_loaders_own_limits():
    page = read("app/ui/pages/security.py")
    assert f"_ld, _lb = capped_window(days, bounds, {_LOGIN_CAP})" in page
    assert f"_nd, _nb = capped_window(days, bounds, {_NETWORK_CAP})" in page
    assert "lookback=_baseline))" in page and "_baseline = security_sql.NETWORK_BASELINE_DAYS" in page
    maximum, keep = _loader()
    assert keep >= maximum                       # a full-depth fill is never purged by its own run


def test_backfill_loads_the_loaders_maximum():
    """The loader clamps DAYS_BACK and the fact keeps no more than its retention, so the maximum is the
    deepest fill that means anything. A smaller one leaves the new-network gate short."""
    maximum, _keep = _loader()
    assert _backfill_days() == maximum


@pytest.mark.parametrize("days,window,today", [w[1:] for w in _WINDOWS], ids=[w[0] for w in _WINDOWS])
@pytest.mark.parametrize("days_later", [0, 1, 45])
def test_both_login_fact_gates_pass_right_after_the_backfill(monkeypatch, days, window, today, days_later):
    """Every Security window serves the login and new-network facts from the first render after the
    backfill (days_later=0), and the retention purge keeps it that way as the hourly task rolls on."""
    login, network = _gates(monkeypatch, today, days, window, _fact_after_backfill(today, days_later=days_later))
    assert login, "the 30-day login readers fell back to live LOGIN_HISTORY"
    assert network, "the new-network panel fell back to its live LOGIN_HISTORY scan (window + 90 days)"


@pytest.mark.parametrize("days", [7, 90])
def test_the_old_90_day_fill_is_what_this_lock_rejects(monkeypatch, days):
    """Teeth: the pre-fix fill (90 days) fails the new-network gate even at the default 7-day window
    (97 complete days needed), while the 30-day login gate it shares the batch with still passes."""
    today = _TODAYS[0]
    login, network = _gates(monkeypatch, today, days, None, _fact_after_backfill(today, depth=90))
    assert login and not network


def test_the_rebuild_docs_state_the_security_fact_span():
    n = _backfill_days()
    assert f"{n}d security facts" in read("snowflake/rebuild/README.md")
    assert f"{n} days of security facts" in " ".join(read("RUNBOOK.md").split())
    assert f"{n} days of security login/change facts" in " ".join(read("docs/FULL_REBUILD.md").split())
    # the generated rebuild copy carries the same call (test_rebuild_bundle byte-locks the whole file)
    assert f"CALL DBA_MAINT_DB.OVERWATCH.{_PROC}({n});" in read("snowflake/rebuild/04_backfill_365.sql")
