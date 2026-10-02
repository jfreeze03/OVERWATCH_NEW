"""Security page round-1 fixes (cluster c05): served windows, pre-LIMIT totals, honest absence.

Each test drives the real builder or the real page/panel function with recording fakes and fails on
the pre-fix tree (main 04fd374e):

* R1-025 / R1-183  new-network panel: the 90-day baseline sits BEFORE the window (a 90d+ window had none).
* R1-026           Who-changed-what: the live fallback serves the fact's 90-day window, and the clean state
                   names that window and its source.
* R1-182           the capped login / admin / DDL readers honour their cap under calendar bounds too.
* R1-101           the login-fact gate measures density over the window it serves.
* R1-029 / R1-184  Access KPIs read pre-LIMIT totals; the MFA stripe is neutral when posture is unproven.
* R1-028           least-privilege reads carry no LIMIT of their own; floors + an honest empty shortlist.
* R1-030           tag coverage ignores a dropped predecessor's TAG_REFERENCES row.
* R1-100 / R1-181  decision-queue counts, verdict and badge come from the uncapped per-domain totals.
* R1-185 / R1-186  effective-access / admin-grant failures are unavailable (with the error); EA KPIs uncapped.
* R1-187 / R1-188  grant-change and DDL/DCL KPIs + charts cover the whole window, not the LIMIT.
* R1-189           break-glass title/clean state follow the company scope.
* R1-027 / R1-190  the auditor pack manifest states each sheet's served window and flags truncation.
"""

from __future__ import annotations

import io
import operator
import re
import zipfile
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW, LAST_MONTH_WINDOW
from app.core.result import QueryResult
from app.data import security_sql
from app.logic.date_windows import window_bounds

TODAY = date(2026, 9, 30)
_ISO = r"'(\d{4}-\d{2}-\d{2})'"


def _ok(df: pd.DataFrame | None = None, *, truncated: bool = False, source: str = "") -> QueryResult:
    return QueryResult(df=pd.DataFrame() if df is None else df, ok=True, truncated=truncated, source=source)


def _failed(kind: str) -> QueryResult:
    return QueryResult(df=pd.DataFrame(), ok=False, error=f"boom ({kind})", error_kind=kind)


def _offset(sql: str, column: str) -> int:
    m = re.search(re.escape(column) + r" >= DATEADD\('day', -(\d+)", sql)
    assert m, f"no trailing predicate on {column}"
    return int(m.group(1))


# ============================================================ fakes for page / panel renders ====

class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


class _Status(_Ctx):
    def __init__(self):
        self.updates: list[dict] = []

    def update(self, **kw):
        self.updates.append(kw)


class _FakeSt:
    """Just enough streamlit: records captions; every other call is a no-op."""

    def __init__(self, *, button: bool = False):
        self.captions: list[str] = []
        self.session_state: dict = {}
        self._button = button
        self.status_box = _Status()
        self.column_config = SimpleNamespace(DatetimeColumn=lambda *a, **k: None, Column=lambda *a, **k: None)

    def caption(self, text="", *_a, **_k):
        self.captions.append(str(text))

    def selectbox(self, _label, options, index=0, **_k):
        return list(options)[index]

    def toggle(self, *_a, **_k):
        return True

    def button(self, *_a, **_k):
        return self._button

    def columns(self, spec, *_a, **_k):
        return [_Ctx() for _ in range(spec if isinstance(spec, int) else len(spec))]

    def expander(self, *_a, **_k):
        return _Ctx()

    def status(self, *_a, **_k):
        return self.status_box

    def progress(self, *_a, **_k):
        return _Ctx()

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


class _Stop(Exception):
    pass


def _harness(monkeypatch, module, results: dict, *, default=None, button: bool = False, **extra):
    """Patch ``module``'s streamlit + render helpers with recorders. ``results`` maps a run() key PREFIX to a
    result (longest prefix wins) or to a callable(sql) -> result."""
    fake = _FakeSt(button=button)
    seen: dict = {"runs": [], "empty": [], "detail": [], "kpis": [], "tables": [], "headers": [],
                  "captions": fake.captions, "stash": [], "charts": []}

    def _lookup(key: str, sql: str):
        hits = sorted((p for p in results if key.startswith(p)), key=len, reverse=True)
        if not hits:
            return default if default is not None else _ok()
        value = results[hits[0]]
        return value(sql) if callable(value) else value

    def fake_run(sql, *_a, key: str = "", **_kw):
        seen["runs"].append((key, sql))
        return _lookup(key, sql)

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    def fake_header(title, health="", *_a, **k):
        seen["headers"].append((title, health, k.get("badge", "")))

    patches = {
        "st": fake, "run": fake_run, "empty_state": fake_empty, "section_header": fake_header,
        "result_caption": lambda *_a, **_k: None, "panel_help": lambda *_a, **_k: None,
        "kpi_row": lambda items, *_a, **_k: seen["kpis"].append(items),
        "styled_table": lambda df, *_a, **_k: seen["tables"].append(df),
        "entity_nav_table": lambda df, *_a, **_k: seen["tables"].append(df),
        "selectable_table": lambda *_a, **_k: None,
        "guard": lambda res, *_a, **_k: bool(res.ok and not res.empty),
        "with_user_names": lambda df, *_a, **_k: df.assign(USER=df.get("USER_NAME")),
        "user_display_map": lambda *_a, **_k: {},
        "snowsight_profile_column": lambda df, *_a, **_k: (df, {}),
        "stash_section_count": lambda _p, label, count, **_k: seen["stash"].append((label, count)),
        "add_to_case_button": lambda *_a, **_k: None,
        "has_migration": lambda *_a, **_k: False,
        "charts": SimpleNamespace(
            daily_stacked_count=lambda df, *_a, **_k: seen["charts"].append(("daily", df)),
            bar_count=lambda df, *_a, **_k: seen["charts"].append(("by_user", df))),
        **extra,
    }
    for name, value in patches.items():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
    return fake, seen


def _sec():
    import app.ui.pages.security as sec
    return sec


def _sc():
    import app.ui.security_center as sc
    return sc


def _kpi(seen: dict, label: str) -> str:
    for row in seen["kpis"]:
        for item in row:
            if item.get("label") == label:
                return str(item.get("value"))
    raise AssertionError(f"no KPI {label!r} in {seen['kpis']}")


# ===================================================== R1-025 / R1-183: new-network baseline ====

@pytest.mark.parametrize("days", [7, 30, 60, 90, 180, 365])
def test_new_network_baseline_precedes_a_trailing_window(days):
    live = security_sql.new_network_logins(days, "ALL")
    fact = security_sql.new_network_logins_fact(days, "ALL")
    window = min(days, 90)
    assert _offset(live, "F.FIRST_SEEN") == window
    assert _offset(live, "L.EVENT_TIMESTAMP") - window >= 90       # a full 90-day baseline before it
    assert _offset(fact, "f.FIRST_SEEN") == window
    assert _offset(fact, "f.DAY") - window >= 90


@pytest.mark.parametrize("window", [CURRENT_MONTH_WINDOW, LAST_MONTH_WINDOW, CURRENT_YEAR_WINDOW])
def test_new_network_baseline_precedes_a_calendar_window(window):
    bounds = window_bounds(window, TODAY)
    for sql, hist_col, seen_col in (
        (security_sql.new_network_logins(30, "ALL", bounds=bounds), "L.EVENT_TIMESTAMP", "F.FIRST_SEEN"),
        (security_sql.new_network_logins_fact(30, "ALL", bounds=bounds), "f.DAY", "f.FIRST_SEEN"),
    ):
        hist = re.search(re.escape(hist_col) + " >= " + _ISO, sql)
        start = re.search(re.escape(seen_col) + " >= " + _ISO, sql)
        assert hist and start, "the baseline must be anchored at the window start"
        win_start = date.fromisoformat(start.group(1))
        assert date.fromisoformat(hist.group(1)) == win_start - timedelta(days=90)
        assert win_start >= bounds[1] - timedelta(days=90)           # window capped so the baseline exists


# =========================================================== R1-026: DDL twins, one window ====

@pytest.mark.parametrize("days", [30, 60, 90, 180, 365])
def test_ddl_live_fallback_serves_the_fact_window(days):
    live = security_sql.recent_ddl_changes(days, "ALL")
    fact = security_sql.recent_ddl_changes_fact(days, "ALL")
    assert _offset(live, "START_TIME") == _offset(fact, "f.EVENT_TS") == min(days, 90)


def test_ddl_clean_state_names_the_served_window_and_its_source(monkeypatch):
    sec = _sec()
    stale = _ok(pd.DataFrame([{"DOMAIN": "CHANGE RISK", "COVERAGE": "STALE"}]))
    captions: list = []
    _, seen = _harness(monkeypatch, sec, {"sec_change_coverage": stale, "ddl_fact_": _ok(), "ddl_": _ok()},
                       result_caption=lambda res, *_a, **_k: captions.append(res))
    sec._changes_tab("ALL", 90)
    ddl_live = [sql for key, sql in seen["runs"] if key.startswith("ddl_ALL")]
    assert ddl_live and _offset(ddl_live[0], "START_TIME") == 90           # the fallback read 90 days
    (msg,) = [m for k, m in seen["empty"] if "DDL/DCL" in m]
    assert "the last 90 days" in msg                                       # not "in this window"
    assert any(getattr(r, "df", None) is not None and r.empty for r in captions)   # source shown on the all-clear


# ================================================= R1-182: caps hold under calendar bounds ====

_CAP_30 = {
    "failed_logins": lambda b: security_sql.failed_logins(30, "ALL", bounds=b),
    "failed_login_reasons": lambda b: security_sql.failed_login_reasons(30, "ALL", bounds=b),
    "single_factor_logins": lambda b: security_sql.single_factor_logins(30, "ALL", bounds=b),
    "login_takeover_candidates": lambda b: security_sql.login_takeover_candidates(30, "ALL", bounds=b),
    "failed_logins_fact": lambda b: security_sql.failed_logins_fact(30, "ALL", bounds=b),
    "failed_login_reasons_fact": lambda b: security_sql.failed_login_reasons_fact(30, "ALL", bounds=b),
}
_CAP_90 = {
    "admin_role_activity": lambda b: security_sql.admin_role_activity(90, "ALL", bounds=b),
    "recent_ddl_changes": lambda b: security_sql.recent_ddl_changes(90, "ALL", bounds=b),
    "recent_ddl_changes_fact": lambda b: security_sql.recent_ddl_changes_fact(90, "ALL", bounds=b),
    "recent_role_grants": lambda b: security_sql.recent_role_grants(90, bounds=b),
}


@pytest.mark.parametrize("name,cap", [(n, 31) for n in _CAP_30] + [(n, 90) for n in _CAP_90])
def test_capped_readers_hold_their_cap_under_current_year(name, cap):
    builder = {**_CAP_30, **_CAP_90}[name]
    bounds = window_bounds(CURRENT_YEAR_WINDOW, TODAY)
    starts = [date.fromisoformat(d) for d in re.findall(_ISO, builder(bounds))]
    assert starts and min(starts) >= bounds[1] - timedelta(days=cap)       # was 2026-01-01: a 273-day scan


@pytest.mark.parametrize("name", [*_CAP_30, *_CAP_90])
def test_a_whole_last_month_is_never_clipped(name):
    builder = {**_CAP_30, **_CAP_90}[name]
    bounds = window_bounds(LAST_MONTH_WINDOW, TODAY)
    assert f"'{bounds[0].isoformat()}'" in builder(bounds)


# ============================================== Access-tab harness (R1-101, R1-182, R1-029, R1-184) ====

def _fact_days_present(today: date) -> set[date]:
    """FACT_SECURITY_LOGIN_DAILY dense 90..11 days ago, a loader outage 10..2 days ago, yesterday loaded."""
    return {today - timedelta(days=1)} | {today - timedelta(days=n) for n in range(11, 91)}


def _dense_fact(today: date, *, holes: tuple[int, ...] = (), today_loaded: bool = True,
                depth: int = 200) -> set[date]:
    """A FACT_SECURITY_LOGIN_DAILY day set: dense for ``depth`` days back, minus ``holes`` (days ago)."""
    days = {today - timedelta(days=n) for n in range(0 if today_loaded else 1, depth)}
    return days - {today - timedelta(days=h) for h in holes}


_SQL_COMPARE = {"GTE": operator.ge, "GT": operator.gt, "LT": operator.lt, "LTE": operator.le, "EQ": operator.eq}


def _sql_value(node, day: date, today: date):
    """Evaluate one fact row (its ``DAY``) against the small Snowflake expression subset the coverage reader
    uses. Anything else fails loudly, so a reader edit can never be scored by a stale model."""
    from sqlglot import exp

    def ev(n):
        return _sql_value(n, day, today)

    def as_date(value):
        return date.fromisoformat(value) if isinstance(value, str) else value

    if isinstance(node, exp.Paren):
        return ev(node.this)
    if isinstance(node, exp.Column):
        assert node.name.upper() == "DAY", node.sql()
        return day
    if isinstance(node, (exp.CurrentDate, exp.CurrentTimestamp)):
        return today                                  # the account clock (Central) in both spellings
    if isinstance(node, exp.ConvertTimezone):
        return ev(node.args["timestamp"])
    if isinstance(node, exp.Cast):
        return as_date(ev(node.this))
    if isinstance(node, exp.Literal):
        return node.this if node.is_string else int(node.this)
    if isinstance(node, exp.Neg):
        return -ev(node.this)
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.DateAdd):
        assert node.text("unit").upper() == "DAY", node.sql()
        return as_date(ev(node.this)) + timedelta(days=ev(node.expression))
    if isinstance(node, exp.Least):
        return min(as_date(ev(arg)) for arg in (node.this, *node.expressions))
    if isinstance(node, exp.And):
        return bool(ev(node.this)) and bool(ev(node.expression))
    if type(node).__name__ in _SQL_COMPARE:
        return _SQL_COMPARE[type(node).__name__](as_date(ev(node.this)), as_date(ev(node.expression)))
    if isinstance(node, exp.If):
        false = node.args.get("false")
        return ev(node.args["true"]) if ev(node.this) else (ev(false) if false is not None else None)
    raise AssertionError(f"coverage simulator: unmodelled SQL node {type(node).__name__}: {node.sql()}")


def _simulated_coverage(present_for):
    """A callable(sql) -> the row the coverage reader returns over a fact holding ``present_for(today)``.
    The reader's WHERE and its COVERAGE_DAYS expression are EVALUATED from the parsed SQL (not
    pattern-matched), so one model scores the pre- and post-fix readers alike."""
    def coverage(sql: str) -> QueryResult:
        import sqlglot
        from sqlglot import exp

        from app.logic import security as logic
        today = logic.account_today()
        tree = sqlglot.parse_one(sql, dialect="snowflake")
        rows = [d for d in sorted(present_for(today)) if _sql_value(tree.args["where"].this, d, today)]
        cols = {e.alias: e.this for e in tree.expressions}
        distinct = cols["COVERAGE_DAYS"].this
        assert isinstance(distinct, exp.Distinct), "COVERAGE_DAYS must stay a DENSITY (COUNT DISTINCT) count"
        values = {_sql_value(distinct.expressions[0], d, today) for d in rows} - {None}
        return _ok(pd.DataFrame([{"FIRST_DAY": rows[0] if rows else None, "LAST_DAY": rows[-1] if rows else None,
                                  "FACT_ROWS": len(rows), "COVERAGE_DAYS": len(values), "LAST_LOAD": None}]))
    return coverage


def _drive_access(monkeypatch, results: dict, *, days: int = 7, bounds=None, stop_at_view: bool = False):
    sec = _sec()
    batches: list = []

    def fake_batch(specs, *_a, **_k):
        batches.append(specs)
        return {}

    def fake_sections(*_a, **_k):
        if stop_at_view:
            raise _Stop
        return "Authentication"

    _, seen = _harness(monkeypatch, sec, results, run_batch=fake_batch, nested_sections=fake_sections,
                       _render_auth_readiness=lambda *_a, **_k: None, _auth_inventory=lambda *_a, **_k: None)
    try:
        sec._access_tab("ALL", days, bounds=bounds)
    except _Stop:
        pass
    seen["batches"] = batches
    return seen


def test_login_fact_gate_measures_density_over_the_served_window(monkeypatch):
    """R1-101: one 90-day density read used to gate a 7-day window, so a 6-day hole inside it passed."""
    seen = _drive_access(monkeypatch, {"sec_security_": _simulated_coverage(_fact_days_present)}, days=7,
                         stop_at_view=True)
    logins = next(s for s in seen["batches"][-1] if s["key"] == "logins")
    assert "ACCOUNT_USAGE.LOGIN_HISTORY" in logins["sql"]            # the live reader, not the gappy fact
    assert "FACT_SECURITY_LOGIN_DAILY" not in logins["sql"]
    assert "coverage fallback" in logins["source"]


FIRST_OF_MONTH = date(2026, 10, 1)
FIRST_OF_YEAR = date(2027, 1, 1)

#: (id, page days, calendar window, reader cap, baseline lookback, an interior hole in days ago (None: the
#: span holds no complete day), the account's today)
_GATE_SPANS = [
    ("7d", 7, None, 30, 0, 4, TODAY),
    ("30d", 30, None, 30, 0, 4, TODAY),
    ("7d+baseline", 7, None, 90, 90, 50, TODAY),
    ("current-month", 30, CURRENT_MONTH_WINDOW, 30, 0, 4, TODAY),
    ("last-month", 30, LAST_MONTH_WINDOW, 30, 0, 45, TODAY),
    ("current-year", 272, CURRENT_YEAR_WINDOW, 30, 0, 4, TODAY),
    ("current-year+baseline", 272, CURRENT_YEAR_WINDOW, 90, 90, 120, TODAY),
    # the first day of a period-to-date window serves today alone: no complete day to require
    ("first-of-month", 0, CURRENT_MONTH_WINDOW, 30, 0, None, FIRST_OF_MONTH),
    ("first-of-month+baseline", 0, CURRENT_MONTH_WINDOW, 90, 90, 50, FIRST_OF_MONTH),
    ("first-of-year", 0, CURRENT_YEAR_WINDOW, 30, 0, None, FIRST_OF_YEAR),
    ("first-of-year+baseline", 0, CURRENT_YEAR_WINDOW, 90, 90, 50, FIRST_OF_YEAR),
]


@pytest.mark.parametrize("days,window,cap,lookback,hole,today", [g[1:] for g in _GATE_SPANS],
                         ids=[g[0] for g in _GATE_SPANS])
def test_login_fact_gate_rejects_one_interior_hole_with_today_loaded(monkeypatch, days, window, cap,
                                                                     lookback, hole, today):
    """R1-101 follow-up: the trailing count spanned days+1 calendar days (today included) against a
    requirement of ``days``, and the calendar count ran to the range end against a requirement that
    stopped at today. Once the hourly load wrote today, today's row stood in for a missing interior
    day and the gappy fact served under its '(hourly)' label. Count and requirement now cover the
    same complete days (today excluded on both sides).

    The first day of a period-to-date window (no baseline) holds no complete day: the requirement
    was clamped to 1 against a count that can only be 0 there, so even a dense fact never served. It
    is 0 now and freshness decides: the fact serves once today's partition is loaded, live before."""
    from app.logic import security as logic
    monkeypatch.setattr(logic, "account_today", lambda: today)
    served_days, served_bounds = logic.capped_window(days, window_bounds(window, today) if window else None, cap)
    sql = security_sql.security_login_fact_coverage(served_days, bounds=served_bounds, lookback=lookback)
    required = logic.coverage_required_days(served_days, served_bounds, lookback=lookback)
    assert (required == 0) is (hole is None)                          # only a today-only span requires 0 days

    def gate(present: set[date]) -> bool:
        return logic.fact_coverage_complete(_simulated_coverage(lambda _today: present)(sql), required)

    assert gate(_dense_fact(today))                                   # a dense fact still serves
    # today's partition is never required -- unless it IS the whole span: then the unloaded fact holds
    # nothing for it, and the live reader serves rather than an empty fact's fabricated zero
    assert gate(_dense_fact(today, today_loaded=False)) is (hole is not None)
    if hole is not None:
        assert not gate(_dense_fact(today, holes=(hole,)))            # one interior hole, today loaded: live
    yesterday = today - timedelta(days=1)
    yesterday_in_span = (served_bounds is None
                         or served_bounds[0] - timedelta(days=lookback) <= yesterday < served_bounds[1])
    assert gate(_dense_fact(today, holes=(1,))) is not yesterday_in_span   # a missing yesterday is a hole too


@pytest.mark.parametrize("days,window", [(7, None), (30, None), (30, CURRENT_MONTH_WINDOW)])
def test_access_tab_serves_live_logins_over_a_one_day_hole_with_today_loaded(monkeypatch, days, window):
    from app.logic import security as logic
    monkeypatch.setattr(logic, "account_today", lambda: TODAY)
    bounds = window_bounds(window, TODAY) if window else None

    def served(present_for) -> dict:
        seen = _drive_access(monkeypatch, {"sec_security_": _simulated_coverage(present_for)}, days=days,
                             bounds=bounds, stop_at_view=True)
        return {s["key"]: s for s in seen["batches"][-1]}

    specs = served(lambda t: _dense_fact(t, holes=(4,)))
    for key in ("logins", "login_reasons"):
        assert "FACT_SECURITY_LOGIN_DAILY" not in specs[key]["sql"]
        assert "ACCOUNT_USAGE.LOGIN_HISTORY" in specs[key]["sql"]
        assert specs[key]["source"] == "ACCOUNT_USAGE.LOGIN_HISTORY (coverage fallback)"
    specs = served(_dense_fact)                                       # control: no hole, the fact serves
    for key in ("logins", "login_reasons"):
        assert "FACT_SECURITY_LOGIN_DAILY" in specs[key]["sql"]
        assert specs[key]["source"] == "FACT_SECURITY_LOGIN_DAILY (hourly)"


@pytest.mark.parametrize("window,today", [(CURRENT_MONTH_WINDOW, FIRST_OF_MONTH), (CURRENT_YEAR_WINDOW, FIRST_OF_YEAR)],
                         ids=["first-of-month", "first-of-year"])
def test_access_tab_first_day_of_period_serves_the_fact_once_today_loads(monkeypatch, window, today):
    """On the 1st of the month (Jan 1 for the year) the served span is today alone. The gate asked for
    one complete day the count could never hold, so a dense, fresh fact always fell back to live."""
    from app.logic import security as logic
    from app.logic.date_windows import resolve_window_days
    monkeypatch.setattr(logic, "account_today", lambda: today)

    def served(present_for) -> dict:
        seen = _drive_access(monkeypatch, {"sec_security_": _simulated_coverage(present_for)},
                             days=resolve_window_days(window, today), bounds=window_bounds(window, today),
                             stop_at_view=True)
        return {s["key"]: s for s in seen["batches"][-1]}

    specs = served(_dense_fact)                                       # today's partition loaded: the fact
    for key in ("logins", "login_reasons"):
        assert "FACT_SECURITY_LOGIN_DAILY" in specs[key]["sql"]
        assert specs[key]["source"] == "FACT_SECURITY_LOGIN_DAILY (hourly)"
    specs = served(lambda t: _dense_fact(t, today_loaded=False))      # not loaded yet: live, never an empty fact
    for key in ("logins", "login_reasons"):
        assert "ACCOUNT_USAGE.LOGIN_HISTORY" in specs[key]["sql"]
        assert specs[key]["source"] == "ACCOUNT_USAGE.LOGIN_HISTORY (coverage fallback)"


def _mfa_unproven_results() -> dict:
    return {"mfa_ALL": _ok(), "mfa_live_ALL": _failed("timeout"), "sec_login_fact_coverage": _ok()}


def test_mfa_stripe_is_neutral_when_posture_is_unproven(monkeypatch):
    """R1-184: an ok+empty mart with a failed live proof painted the MFA stripe green over 'unconfirmed'."""
    seen = _drive_access(monkeypatch, _mfa_unproven_results())
    (mfa,) = [h for h in seen["headers"] if h[0].startswith("MFA gaps")]
    assert mfa[1] == ""                                              # neutral, never "ok"
    assert ("no_data_yet", next(m for k, m in seen["empty"] if "MFA posture is unconfirmed" in m)) in seen["empty"]


def test_access_kpis_read_pre_limit_totals(monkeypatch):
    """R1-029: the MFA badge, single-factor KPIs and takeover KPIs saturated at the builders' LIMITs."""
    mfa = pd.DataFrame({"USER_NAME": [f"U{i}" for i in range(200)], "PASSWORD_LOGINS_30D": 1,
                        "TOTAL_USERS_WIN": 640})
    sf = pd.DataFrame({"USER_NAME": [f"S{i}" for i in range(200)], "HAS_MFA": [i < 50 for i in range(200)],
                       "TOTAL_USERS_WIN": 450, "TOTAL_ENROLLED_WIN": 120})
    ato = pd.DataFrame({"USER_NAME": [f"A{i}" for i in range(100)], "FAILURES": 6, "FAIL_IPS": 1,
                        "SUCCEEDED_AFTER": [i < 10 for i in range(100)], "FIRST_FAILURE": None,
                        "FIRST_SUCCESS_AFTER": None, "BREAKTHROUGH_MIN": None, "LAST_ERROR": "x",
                        "TOTAL_BURSTS_WIN": 260, "TOTAL_BROKE_WIN": 31, "TOTAL_HIGH_WIN": 7})
    nn = pd.DataFrame({"USER_NAME": [f"N{i}" for i in range(200)], "CLIENT_IP": "10.0.0.1", "TOTAL_PAIRS_WIN": 350})
    seen = _drive_access(monkeypatch, {"mfa_ALL": _ok(mfa), "single_factor_": _ok(sf), "takeover_": _ok(ato),
                                       "newnet_": _ok(nn), "sec_login_fact_coverage": _ok()})
    assert any(c.startswith("350 new (user, network) pair(s)") and "Showing the newest 200" in c
               for c in seen["captions"])
    (mfa_hdr,) = [h for h in seen["headers"] if h[0].startswith("MFA gaps")]
    assert mfa_hdr[2] == "640 to fix"
    assert _kpi(seen, "Users with single-factor logins") == "450"
    assert _kpi(seen, "Enrolled yet single-factor") == "120"
    assert _kpi(seen, "Failure bursts") == "260"
    assert _kpi(seen, "Succeeded after (breakthrough)") == "31"
    assert _kpi(seen, "High severity") == "7"
    for table in seen["tables"]:                                     # helper totals never displayed
        assert not set(security_sql.WINDOW_TOTAL_COLUMNS) & set(table.columns)


def test_single_factor_header_and_clean_states_name_the_served_window(monkeypatch):
    """R1-182: the header said '30d' under a 7-day window; the clean states claimed a 30d cap that
    calendar windows bypassed."""
    seen = _drive_access(monkeypatch, {"sec_login_fact_coverage": _ok()}, days=7)
    assert any(h[0] == "Single-factor logins (MFA-bypassed, 7d)" for h in seen["headers"])
    assert ("clean", "No failed logins in the last 7 days.") in seen["empty"]
    bounds = window_bounds(CURRENT_YEAR_WINDOW, TODAY)
    seen = _drive_access(monkeypatch, {"sec_login_fact_coverage": _ok()}, days=272, bounds=bounds)
    served = [m for k, m in seen["empty"] if m.startswith("No failed logins in Aug 31 - Sep 30")]
    assert len(served) == 2                                          # failed logins + their reasons
    # the cap note states the SERVED span (a calendar window keeps a whole month: 31 days), never a
    # '30 days' beside a printed 31-day range
    assert "No failed logins in Aug 31 - Sep 30 (this reader is capped at the last 31 days)." in served
    assert not any("capped at 30 days" in m for m in served)
    seen = _drive_access(monkeypatch, {"sec_login_fact_coverage": _ok()}, days=90)
    assert ("clean", "No failed logins in the last 30 days (this reader is capped at the last 30 days).") \
        in seen["empty"]


def test_takeover_high_total_mirrors_takeover_severity():
    """The SQL pre-LIMIT High total uses takeover_severity's rule (parity lock)."""
    from app.logic.insights import takeover_severity
    grid = pd.DataFrame([{"USER_NAME": f"U{f}_{i}_{s}", "FAILURES": f, "FAIL_IPS": i, "SUCCEEDED_AFTER": s}
                         for f in (5, 9, 10, 40) for i in (1, 2, 3, 5) for s in (True, False)])
    ranked = takeover_severity(grid)
    mirrored = ranked["SUCCEEDED_AFTER"] & ((ranked["FAILURES"] >= security_sql.TAKEOVER_HIGH_FAILURES)
                                            | (ranked["FAIL_IPS"] >= security_sql.TAKEOVER_HIGH_FAIL_IPS))
    assert (mirrored == (ranked["SEVERITY"] == "High")).all()
    sql = security_sql.login_takeover_candidates(7, "ALL")
    assert "f.FAILURES >= 10 OR f.FAIL_IPS >= 3" in sql
    assert sql.index("TOTAL_HIGH_WIN") < sql.rindex("LIMIT 100")


# =============================================================== R1-028: least privilege ====

def test_least_privilege_reads_have_no_limit_below_the_row_cap():
    from app.config import DEFAULT_MAX_ROWS
    from app.core.query import _with_row_cap
    for sql in (security_sql.grant_scope_usage(90), security_sql.unused_table_grants(90)):
        # run()'s n+1 check can now arm res.truncated (a builder LIMIT 500 was kept and masked it)
        assert _with_row_cap(sql, DEFAULT_MAX_ROWS).rstrip().endswith(f"LIMIT {DEFAULT_MAX_ROWS + 1}")
    unused = security_sql.unused_table_grants(90)
    order = unused[unused.rindex("ORDER BY"):]
    assert order.index("SCOPE_UNUSED_GRANTS DESC") < order.index("g.ROLE_NAME")   # worst scope, not alphabet


def _drive_least_privilege(monkeypatch, scopes: QueryResult, unused: QueryResult):
    sec = _sec()
    cov = _ok(pd.DataFrame([{"COVERAGE_DAYS": 90, "LATEST_DAYS_AGO": 1}]))
    _, seen = _harness(monkeypatch, sec, {"sec_lp_cov": cov, "sec_lp_scopes": scopes, "sec_lp_unused": unused},
                       selectable_table=lambda *_a, **_k: 0)
    sec._least_privilege_tab()
    return seen


def test_least_privilege_kpis_are_floors_and_an_emptied_scope_is_an_absence(monkeypatch):
    scopes = pd.DataFrame([{"ROLE_NAME": "ZED_ROLE", "DATABASE_NAME": "DB", "SCHEMA_NAME": "S",
                            "GRANTED_TABLES": 9, "TOUCHED_TABLES": 0, "TOTAL_SCOPES_WIN": 7000}])
    unused = pd.DataFrame([{"ROLE_NAME": "ALPHA", "PRIVILEGE": "SELECT", "OBJECT_NAME": "DB2.S.T",
                            "SCOPE_UNUSED_GRANTS": 4000, "TOTAL_GRANTS_WIN": 9000}])
    seen = _drive_least_privilege(monkeypatch, _ok(scopes, truncated=True), _ok(unused, truncated=True))
    assert _kpi(seen, "Unused scopes") == "1+"                         # a floor once run()'s cap fires
    assert _kpi(seen, "Roles reviewed") == "1+"
    # the selected ZED_ROLE scope filters the loaded shortlist to nothing: an absence outcome, never a
    # bare empty table (and it says the shortlist was cut)
    assert any(k == "no_data_yet" and "hit the row cap" in m for k, m in seen["empty"])
    assert all(not t.empty for t in seen["tables"])
    assert not any("TOTAL_SCOPES_WIN" in t.columns or "TOTAL_GRANTS_WIN" in t.columns for t in seen["tables"])


# =================================================================== R1-030: tag coverage ====

def test_tag_coverage_ignores_a_dropped_predecessors_tag_reference():
    sqlglot = pytest.importorskip("sqlglot")
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE TABLES (TABLE_ID INT, TABLE_CATALOG TEXT, TABLE_SCHEMA TEXT, TABLE_NAME TEXT, "
               "TABLE_TYPE TEXT, DELETED TEXT, ROW_COUNT INT, BYTES INT)")
    db.execute("CREATE TABLE TAG_REFERENCES (TAG_NAME TEXT, DOMAIN TEXT, OBJECT_ID INT, OBJECT_DATABASE TEXT, "
               "OBJECT_SCHEMA TEXT, OBJECT_NAME TEXT, OBJECT_DELETED TEXT)")
    # DB.S.T was tagged (id 1), then CREATE OR REPLACE'd without COPY TAGS: id 1 dropped, id 2 live + untagged
    db.executemany("INSERT INTO TABLES VALUES (?,?,?,?,?,?,?,?)", [
        (1, "DB", "S", "T", "BASE TABLE", "2026-09-29", 10, 100),
        (2, "DB", "S", "T", "BASE TABLE", None, 10, 100),
        (3, "DB", "S", "U", "BASE TABLE", None, 10, 50)])
    db.execute("INSERT INTO TAG_REFERENCES VALUES ('COST_OWNER','TABLE',1,'DB','S','T','2026-09-29')")

    def lite(sql: str) -> str:
        return sqlglot.transpile(sql.replace("SNOWFLAKE.ACCOUNT_USAGE.", ""), read="snowflake", write="sqlite")[0]

    coverage = db.execute(lite(security_sql.object_tag_coverage("ALL", tags=("COST_OWNER",)))).fetchall()
    (row,) = coverage
    assert 0 in row and 2 in row                                      # 0 of 2 live tables tagged
    untagged = {r[0] for r in db.execute(lite(security_sql.untagged_objects("ALL", "COST_OWNER"))).fetchall()}
    assert "DB.S.T" in untagged                                       # the replaced table is on the worklist
    assert "OBJECT_DELETED" in security_sql.object_tag_probe()        # the probe proves the column first


# ===================================================== R1-100 / R1-181: decision-queue counts ====

def _capped_queue() -> pd.DataFrame:
    change = pd.DataFrame({"DOMAIN": "CHANGE RISK", "SEVERITY": "HIGH", "TITLE": [f"c{i}" for i in range(100)],
                           "IMPACT_COUNT": 1, "DOMAIN_ROWS": 4024, "DOMAIN_FINDINGS": 4024})
    ident = pd.DataFrame({"DOMAIN": "IDENTITY", "SEVERITY": "MEDIUM", "TITLE": ["i0", "i1", "i2"],
                          "IMPACT_COUNT": [12, 3, 5], "DOMAIN_ROWS": 3, "DOMAIN_FINDINGS": 20})
    return pd.concat([change, ident], ignore_index=True).assign(TOTAL_ROWS=4027)


def test_domain_posture_counts_the_uncapped_domain_findings():
    from app.logic.security import domain_posture
    cov = pd.DataFrame({"DOMAIN": ["CHANGE RISK", "IDENTITY"], "COVERAGE": "COMPLETE"})
    posture = {p.domain: p for p in domain_posture(_capped_queue(), cov)}
    assert posture["CHANGE RISK"].findings == 4024                     # was 100 (the per-domain LIMIT)
    assert posture["IDENTITY"].findings == 20
    sql = security_sql.security_exception_queue("ALL", 100)
    for col in ("DOMAIN_ROWS", "DOMAIN_FINDINGS", "TOTAL_ROWS"):
        assert sql.index(col) < sql.index("QUALIFY")                  # computed before the cap


def test_decision_queue_badge_kpis_and_caption_are_uncapped(monkeypatch):
    sc = _sc()
    cov = _ok(pd.DataFrame({"DOMAIN": ["CHANGE RISK", "IDENTITY"], "COVERAGE": "COMPLETE"}))
    _, seen = _harness(monkeypatch, sc, {"sec_exception_queue_ALL": _ok(_capped_queue()), "sec_domain_coverage": cov},
                       _render_change_risk_diagnostic=lambda: None)
    sc.render_security_overview("ALL")
    assert seen["stash"] == [("Decision queue", 4027)]                  # was len(frame) = 103
    deltas = {i["label"]: i["delta"] for i in seen["kpis"][0]}
    assert deltas["Change Risk"].endswith("| 4,024 open")
    assert any("Showing 103 of 4,027 queued exceptions" in c and "Change Risk: 100 of 4,024" in c
               for c in seen["captions"])
    verdict = sc.security_posture_verdict("ALL")
    assert "4,044 open finding(s)" in verdict["body"]


# =========================================== R1-185 / R1-186: effective access + admin grants ====

_SETUP = ("absent", "privilege", "unknown_function")
_FAILED = ("missing_column", "timeout", "other")


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
@pytest.mark.parametrize("panel,key", [("render_effective_access", "sec_effective_ALL"),
                                       ("render_admin_grant_anomalies", "sec_admin_grant_ctx_ALL")])
def test_grant_panels_render_a_failed_read_by_its_kind(monkeypatch, panel, key, kind):
    sc = _sc()
    _, seen = _harness(monkeypatch, sc, {key: _failed(kind)})
    getattr(sc, panel)("ALL")
    ((state, _msg),) = seen["empty"]
    if kind in _SETUP:
        assert state == "needs_setup"
    else:
        assert state == "unavailable" and seen["detail"] == [f"boom ({kind})"]   # was a quiet no_data_yet
    assert not seen["kpis"]


def _paths(n_users: int, per_user: int) -> pd.DataFrame:
    rows = [{"USER_NAME": f"U{u:03d}", "DIRECT_ROLE": f"R{u}", "EFFECTIVE_ROLE": f"E{u}_{p}", "DEPTH": 1,
             "ACCESS_PATH": "x", "PRIVILEGES": 1, "OWNERSHIP_GRANTS": 10, "MANAGE_GRANTS": 0,
             "SENSITIVE_PRIVILEGES": 0, "RISK_SCORE": 100, "REACHES_ADMIN": False, "USER_PATH_RANK": p + 1}
            for u in range(n_users) for p in range(per_user)]
    return pd.DataFrame(rows)


def test_effective_access_kpis_read_the_uncapped_totals(monkeypatch):
    sc = _sc()
    frame = _paths(30, 100).assign(TOTAL_PATHS_WIN=4500, TOTAL_PATH_USERS_WIN=241,
                                   TOTAL_HIGH_RISK_USERS_WIN=40, TOTAL_SELF_ESCALATORS_WIN=1)
    _, seen = _harness(monkeypatch, sc, {"sec_effective_ALL": _ok(frame)})
    sc.render_effective_access("ALL")
    assert _kpi(seen, "Effective paths") == "4,500"                     # was 3,000 (the LIMIT)
    assert _kpi(seen, "Users") == "241"
    assert _kpi(seen, "High-risk users") == "40"
    assert _kpi(seen, "Can self-escalate to admin") == "1"              # outside the kept rows, still counted
    assert any("Showing 3,000 of 4,500 effective paths" in c for c in seen["captions"])
    for table in seen["tables"]:
        assert not set(security_sql.WINDOW_TOTAL_COLUMNS) & set(table.columns)


def test_effective_access_keeps_every_users_best_path_ahead_of_the_cap():
    sql = security_sql.effective_access("ALL")
    order = sql[sql.rindex("ORDER BY"):]
    assert order.index("IFF(p.USER_PATH_RANK = 1, 0, 1)") < order.index("IFF(p.MANAGE_GRANTS > 0 OR p.REACHES_ADMIN")
    assert "COUNT(DISTINCT USER_NAME) AS TOTAL_PATH_USERS_WIN" in sql and "CROSS JOIN totals" in sql


# =================================================== R1-187 / R1-188 / R1-189: Changes tab ====

def _drive_changes(monkeypatch, results: dict, company: str = "ALL", days: int = 90):
    sec = _sec()
    _, seen = _harness(monkeypatch, sec, results)
    sec._changes_tab(company, days)
    return seen


def test_grant_change_kpis_read_the_pre_limit_totals(monkeypatch):
    g = pd.DataFrame({"CHANGED_AT": pd.Timestamp("2026-09-01"), "CHANGE": ["GRANTED"] * 400 + ["REVOKED"] * 100,
                      "GRANT_TYPE": "ROLE", "CHANGED_BY": "X", "GRANTEE": "U", "WHAT": "R",
                      "TOTAL_CHANGES_WIN": 1234, "GRANTED_WIN": 1000, "REVOKED_WIN": 234})
    seen = _drive_changes(monkeypatch, {"grant_changes_feed_": _ok(g)})
    assert _kpi(seen, "Grant changes, 30d") == "1,234"                  # was 500
    assert _kpi(seen, "Granted") == "1,000" and _kpi(seen, "Revoked") == "234"
    assert any("Showing the newest 500 of 1,234 changes" in c for c in seen["captions"])
    assert "TOTAL_CHANGES_WIN" in security_sql.recent_grant_changes(90, "ALL")


def _ddl_groups(n: int) -> pd.DataFrame:
    return pd.DataFrame({"DAY": pd.Timestamp("2026-09-29"), "USER_NAME": "NEWEST", "ROLE_NAME": "R",
                         "QUERY_TYPE": "CREATE_TABLE", "DATABASE_NAME": "DB", "SCHEMA_NAME": "S",
                         "STATEMENTS": 1, "LAST_CHANGE": pd.Timestamp("2026-09-29"), "RISK_LEVEL": "LOW",
                         "CHANGE_REGISTRATION": "REGISTERED", "TOTAL_GROUPS_WIN": 900,
                         "HIGH_RISK_GROUPS_WIN": 57, "UNREGISTERED_GROUPS_WIN": 12},
                        index=range(n))


def test_ddl_kpis_and_charts_cover_the_whole_window(monkeypatch):
    covered = _ok(pd.DataFrame([{"DOMAIN": "CHANGE RISK", "COVERAGE": "COMPLETE"}]))
    rollup = _ok(pd.DataFrame([
        {"GRAIN": "DAY_TYPE", "DAY": pd.Timestamp("2026-07-15"), "QUERY_TYPE": "DROP", "USER_NAME": None,
         "STATEMENTS": 40},
        {"GRAIN": "DAY_TYPE", "DAY": pd.Timestamp("2026-09-29"), "QUERY_TYPE": "CREATE_TABLE", "USER_NAME": None,
         "STATEMENTS": 300},
        {"GRAIN": "USER", "DAY": None, "QUERY_TYPE": None, "USER_NAME": "OLDEST", "STATEMENTS": 40},
        {"GRAIN": "USER", "DAY": None, "QUERY_TYPE": None, "USER_NAME": "NEWEST", "STATEMENTS": 300}]))
    # the fact serves only over a span it holds (CHANGE RISK COMPLETE is freshness only, R2-006 follow-up)
    seen = _drive_changes(monkeypatch, {"sec_change_coverage": covered, "ddl_fact_": _ok(_ddl_groups(300)),
                                        "sec_change_fact_span_": _simulated_coverage(_dense_fact),
                                        "ddl_rollup_fact_": rollup})
    assert _kpi(seen, "High-risk change groups") == "57"                # was 0 (counted from the newest 300)
    assert _kpi(seen, "Unregistered groups") == "12"
    assert any(key.startswith("ddl_rollup_fact_") for key, _ in seen["runs"])
    daily = dict(seen["charts"])["daily"]
    assert pd.Timestamp("2026-07-15") in set(daily["DAY"])              # the oldest day is still charted
    assert "OLDEST" in set(dict(seen["charts"])["by_user"]["USER_NAME"])
    assert any("newest 300 of 900 change groups" in c for c in seen["captions"])


def test_ddl_totals_are_counted_after_the_registry_dedupe():
    for sql in (security_sql.recent_ddl_changes(30, "ALL"), security_sql.recent_ddl_changes_fact(30, "ALL")):
        assert sql.index("QUALIFY ROW_NUMBER()") < sql.index("COUNT(*) OVER () AS TOTAL_GROUPS_WIN")
        assert "FROM final f" in sql and sql.rstrip().endswith("LIMIT 300")


def test_breakglass_label_follows_the_company_scope(monkeypatch):
    seen = _drive_changes(monkeypatch, {"breakglass_": _ok()}, company="ALFA", days=30)
    (title,) = [h[0] for h in seen["headers"] if h[0].startswith("Break-glass")]
    assert "account-wide" not in title and "ALFA-classified users" in title
    (msg,) = [m for k, m in seen["empty"] if "ACCOUNTADMIN" in m]
    assert "ALFA-classified users" in msg and "Switch Company to ALL" in msg
    seen = _drive_changes(monkeypatch, {"breakglass_": _ok()}, company="ALL", days=30)
    assert any(h[0] == "Break-glass role activity (account-wide; should hug zero)" for h in seen["headers"])


# ============================================================ R1-027 / R1-190: export pack ====

def _drive_pack(monkeypatch, days: int, truncated: tuple[str, ...] = (), *, bounds=None):
    sec = _sec()
    import app.ui.components as components
    specs: list = []

    def fake_batch(items, *_a, **_k):
        specs.extend(items)
        out = {}
        for item in items:
            df = pd.DataFrame({"USER_NAME": ["U1", "U2"], "TOTAL_USERS_WIN": 2})
            out[item["key"]] = _ok(df, truncated=item["key"] in truncated)
        return out

    monkeypatch.setattr(components, "log_ui_event", lambda *_a, **_k: None)
    fake, _ = _harness(monkeypatch, sec, {}, button=True, run_batch=fake_batch,
                       cache_scope=lambda: "s", export_button=lambda *_a, **_k: None)
    sec._export_pack("ALL", days, f"Last {days} days", bounds=bounds)
    blob = fake.session_state["_ow_security_pack"]["data"]
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        manifest = z.read("MANIFEST.txt").decode("utf-8")
        mfa_csv = z.read("mfa_gaps_password_login.csv").decode("utf-8")
    lines = {ln.split(".csv:")[0]: ln for ln in manifest.splitlines() if ".csv:" in ln}
    return {s["key"]: s for s in specs}, manifest, lines, mfa_csv, fake


def test_pack_manifest_states_each_windowed_sheets_served_span(monkeypatch):
    _, _, lines, mfa_csv, _ = _drive_pack(monkeypatch, 90)
    assert "covers the last 30 days (reader capped at the last 30 days" in lines["failed_logins_window"]
    assert "covers the last 90 days" in lines["role_grants_window"]
    assert "capped" not in lines["role_grants_window"]
    _, _, lines, _, _ = _drive_pack(monkeypatch, 365)
    assert "covers the last 90 days (reader capped at the last 90 days" in lines["role_grants_window"]
    assert "TOTAL_USERS_WIN" not in mfa_csv                              # KPI helper never exported


def test_pack_manifest_cap_note_matches_the_served_calendar_span(monkeypatch):
    """R1-182 follow-up: under 'Current year' the 30-day reader keeps a whole month (31 days), so the note
    said 'capped at 30 days' beside a printed 31-day range."""
    _, _, lines, _, _ = _drive_pack(monkeypatch, 272, bounds=window_bounds(CURRENT_YEAR_WINDOW, TODAY))
    assert lines["failed_logins_window"].endswith(
        "covers Aug 31 - Sep 30 (reader capped at the last 31 days — narrower than the page window)")
    assert lines["role_grants_window"].endswith(
        "covers Jul 3 - Sep 30 (reader capped at the last 90 days — narrower than the page window)")


def test_pack_flags_truncated_sheets(monkeypatch):
    specs, manifest, lines, _, fake = _drive_pack(
        monkeypatch, 30, truncated=("direct_role_grants", "dormant_users"))
    # each sheet is fetched at its OWN LIMIT (or the pack cap), so run()'s n+1 check can arm
    assert specs["dormant_users"]["max_rows"] == 300                     # insights_sql LIMIT 300
    assert specs["direct_role_grants"]["max_rows"] == 10_000
    assert specs["direct_role_grants"]["sql"].rstrip().endswith("LIMIT 10000")
    assert specs["role_privilege_matrix"]["sql"].rstrip().endswith("LIMIT 10000")
    assert "TRUNCATED" in lines["direct_role_grants"] and "TRUNCATED" in lines["dormant_users"]
    assert "TRUNCATED" not in lines["grant_changes_90d"]
    assert "TRUNCATED SHEETS" in manifest
    assert fake.status_box.updates[-1]["label"] == "Evidence pack built with gaps"
