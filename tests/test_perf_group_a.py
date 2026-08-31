"""Group A perf/telemetry batch: P3 per-member batch timing, P1 one-pass
health strip, D5 EST_WAIT_S, C6 page-scoped slow keys, K1 served_days,
K2 cs_by_query_type_mart."""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest

import app.core.query as q
from app.data import cost_sql, mart_sql
from app.ui.components import served_days

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# P3 — every batch member used to record the WHOLE batch's wall time
# ---------------------------------------------------------------------------

class _FakeJob:
    def __init__(self, df=None, delay=0.0):
        self._df, self._delay = df, delay
        self.query_id = "qid"

    def result(self):
        if self._delay:
            time.sleep(self._delay)
        return self._df


class _FakeSQL:
    def __init__(self, job):
        self._job = job

    def to_pandas(self, block=True):
        return self._job


class _FakeSession:
    def __init__(self, jobs):
        self._jobs = list(jobs)

    def sql(self, _s):
        return _FakeSQL(self._jobs.pop(0))


@pytest.fixture
def _quiet(monkeypatch):
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q, "apply_statement_timeout", lambda *a, **k: None)
    monkeypatch.setattr(q, "record_error", lambda *a, **k: None)


def test_batch_members_are_timed_individually(monkeypatch, _quiet):
    df = pd.DataFrame({"A": [1]})
    monkeypatch.setattr(q, "get_session", lambda: _FakeSession(
        [_FakeJob(df=df, delay=0.20), _FakeJob(df=df)]))
    q._execute_batch(("s0", "s1"), "recent", "T")
    ms = q._BATCH_MEMBER_MS.get()
    assert ms is not None and set(ms) == {0, 1}
    assert ms[0] > 150      # the slow member owns its own wait
    assert ms[1] < 50       # the fast one is NOT stamped with the batch wall clock


def test_run_batch_writes_per_member_plus_one_wall_row(monkeypatch, _quiet):
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(q, "_telemetry",
                        lambda page, tier, key, ms, rows, ok, **kw: seen.append((key, ms)))
    df = pd.DataFrame({"A": [1]})
    monkeypatch.setattr(q, "get_session", lambda: _FakeSession(
        [_FakeJob(df=df, delay=0.20), _FakeJob(df=df)]))
    monkeypatch.setattr(q, "_BATCH_FETCHERS",
                        {"recent": lambda sqls, scope, page: q._execute_batch(sqls, "recent", page)})
    out = q.run_batch([{"key": "slow", "sql": "s0"}, {"key": "fast", "sql": "s1"}],
                      page="T", tier="recent")
    keys = dict(seen)
    assert keys["batch:slow"] > 150 and keys["batch:fast"] < 50
    assert len([k for k in keys if k.startswith("batch_wall:")]) == 1   # ONE wall row
    assert out["slow"].elapsed_ms > 150 and out["fast"].elapsed_ms < 50


def test_stale_member_timings_cannot_leak_into_a_cache_hit(monkeypatch, _quiet):
    """A cache HIT never runs _execute_batch, so the previous batch's dict must
    not be read as this one's — run_batch clears the sentinel first."""
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(q, "_telemetry",
                        lambda page, tier, key, ms, rows, ok, **kw: seen.append((key, ms)))
    q._BATCH_MEMBER_MS.set({0: 9999.0})
    df = pd.DataFrame({"A": [1]})
    monkeypatch.setattr(q, "_BATCH_FETCHERS", {"recent": lambda sqls, scope, page: (df,)})
    q.run_batch([{"key": "k", "sql": "s"}], page="T", tier="recent")
    assert dict(seen)["batch:k"] < 100      # wall time of a hit, not the stale 9999


# ---------------------------------------------------------------------------
# P1 — one scan per source table, identical output contract
# ---------------------------------------------------------------------------

def test_health_strip_is_single_pass_but_keeps_its_metric_contract():
    sql = mart_sql.health_strip()
    for metric in ("OPEN_CRITICAL", "UNDELIVERED_CRITICAL", "STALEST_SOURCE_H",
                   "STALEST_SOURCE_NAME", "MTD_CREDITS", "MTD_CREDITS_AI",
                   "MTD_CREDITS_OTHER", "STALE_SOURCES"):
        assert f"'{metric}'" in sql          # main.py parses these BY NAME
    assert "METRIC" in sql and "VALUE" in sql and "STATE" in sql
    # one scan per FRESHNESS/METERING source (the old build re-scanned 2/3/3 times)
    assert sql.count("OVERWATCH.SOURCE_FRESHNESS_STATE") == 1
    assert sql.count("OVERWATCH.FACT_METERING_DAILY") == 1
    # alert-hunt #6: the undelivered-critical check is now ROUTE-level (flag a CRITICAL
    # missing delivery to an ELIGIBLE enabled route, not merely with zero delivery rows),
    # which needs a small critical-only anti-join over ALERT_EVENTS in und_crit besides
    # the main crit aggregate. All arms filter to OPEN CRITICALs, so it's a cheap filtered
    # subset, not a full re-scan; and it stays a dedup UNION, never UNION ALL.
    assert "und_crit AS" in sql and "u.EVENT_ID IS NOT NULL" in sql
    assert sql.count("OVERWATCH.ALERT_EVENTS") == 3      # crit aggregate + und_crit's 2 arms
    assert "UNION ALL" not in sql


def test_shell_health_read_has_its_own_ttl_and_invalidation():
    m = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "@st.cache_data(ttl=120" in m
    assert "cache_scope(mart_sql.health_strip())" in m   # Refresh/acks still reach it
    assert 'tier="live"' in m                            # underlying entry stays shared


# ---------------------------------------------------------------------------
# D5 / C6 / P11 — the Admin tuning board
# ---------------------------------------------------------------------------

def test_telemetry_by_page_exposes_reweighted_wait_and_excludes_wall_rows():
    sql = mart_sql.telemetry_by_page(7)
    assert "AS EST_WAIT_S" in sql
    assert "COALESCE(SAMPLE_PROB, 1.0)" in sql
    # batch_wall rows are a superset of their members — summing both double-counts
    assert "STARTSWITH(QUERY_KEY, 'batch_wall:')" in sql
    assert "AS P95_S" in sql                       # p95 kept as a column
    assert "ORDER BY EST_WAIT_S DESC" in sql


def test_pain_board_ranks_on_wait_and_drops_zero_pain_rows():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert '_tt["PAIN"] = _tt["EST_WAIT_S"]' in adm
    assert '_tt[_tt["PAIN"] > 0]' in adm
    assert "Sub-2s pain is invisible here" in adm and "exception-weighted" in adm


def test_pain_drill_asks_for_the_page_before_claiming_nothing_is_slow():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "mart_sql.fleet_query_stats(7, page=_pg)" in adm
    assert "PAGE = " in mart_sql.fleet_query_stats(7, page="Cost")
    assert "PAGE = " not in mart_sql.fleet_query_stats(7)


def test_app_statement_stats_serves_from_telemetry_with_the_scan_behind_a_toggle():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "mart_sql.app_statement_stats_telemetry(7)" in adm
    assert 'key="adm_stmt_scan"' in adm
    sql = mart_sql.app_statement_stats_telemetry(7)
    assert "APP_QUERY_TELEMETRY" in sql and "QUERY_HISTORY" not in sql
    assert "MAX_BY(QUERY_ID, IFF(QUERY_ID IS NOT NULL, ELAPSED_MS, NULL))" in sql


# ---------------------------------------------------------------------------
# D7 — the untagged denominator behind a threshold suggestion
# ---------------------------------------------------------------------------

def test_rule_metric_kinds_carries_the_untagged_count():
    sql = mart_sql.rule_metric_kinds(90)
    assert "AS UNTAGGED_N" in sql
    assert "COALESCE(RESOLUTION_KIND, '') = ''" in sql       # same spelling as alert_fatigue
    assert "r.RESOLUTION_KIND IN ('ACTIONED', 'NOISE')" in sql   # the sample is unchanged


# ---------------------------------------------------------------------------
# K1 / K2 — the cross-group contracts
# ---------------------------------------------------------------------------

class _Res:
    def __init__(self, attrs=None):
        self.df = pd.DataFrame({"A": [1]})
        self.df.attrs.update(attrs or {})


def test_served_days_reports_the_window_that_actually_answered():
    assert served_days(_Res(), 365) == 365                       # not a run_mart_first result
    assert served_days(_Res({"_ow_served_live": False,
                             "_ow_effective_days": 365}), 365) == 365
    assert served_days(_Res({"_ow_served_live": True,
                             "_ow_effective_days": 90}), 365) == 90
    # attrs-less live marker still clamps rather than believing the request
    assert served_days(_Res({"_ow_served_live": True}), 365) == 90


def test_run_mart_first_stamps_every_return_path():
    src = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    body = src.split("def run_mart_first(", 1)[1].split("\ndef ", 1)[0]
    # accepted mart / live-after-reject / partial-mart / empty-is-answer / live fallback
    assert body.count("_mark_served(") == 5
    assert "return res\n" not in body                # no unstamped path left


def test_cs_by_query_type_mart_matches_the_live_output_contract():
    live = cost_sql.cs_by_query_type(30, "ALFA")
    mart = mart_sql.cs_by_query_type_mart(30, "ALFA")
    for col in ("QUERY_TYPE", "AS QUERIES", "AS CS_CREDITS", "AS CS_CREDITS_PER_1K"):
        assert col in live and col in mart
    assert "MART_CLOUD_SVC_DAILY" in mart and "QUERY_HISTORY" not in mart
    assert "ORDER BY CS_CREDITS DESC" in mart and "LIMIT 12" in mart


def test_user_directory_gets_a_day_long_ttl_without_pinning_failures():
    src = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert "@st.cache_data(ttl=86400" in src
    assert "raise _DirectoryUnavailable(" in src      # a failed read is never cached
    assert "except _DirectoryUnavailable:\n        return {}" in src


def test_account_usage_probe_no_longer_scans_query_history():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    probes = adm.split("probes = [", 1)[1].split("]", 1)[0]
    # comments legitimately name the view they replaced — check the SQL only
    stmts = "\n".join(ln for ln in probes.splitlines() if not ln.lstrip().startswith("#"))
    assert "SNOWFLAKE.ACCOUNT_USAGE.DATABASES LIMIT 1" in stmts
    assert "QUERY_HISTORY" not in stmts
