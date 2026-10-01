"""v4.608 (PR-3, cluster e5-cost): Cost Intelligence ▸ Spend & Attribution, plus two mart_sql leads.

  R2-076  a failed prior-month storage read (fact + live fallback) rendered 'Prior full month = 0.00 TiB · no
          prior data': a fabricated zero and a no-data claim for a read that failed (house law 8);
  lead    the daily anomaly check's how-to caption said "open the waterfall above" when the explanation was
          refused (too little history before the flagged day, R1-070) and no waterfall rendered;
  lead    fleet_query_stats listed 'batch_wall:%' rows (a batch's wall clock, a SUPERSET of its members) as
          slow fetch keys, unlike every other telemetry aggregator;
  lead    rule_precision counted only a NULL kind as UNTAGGED, so an empty-string kind fell out of every bucket
          while RESOLVED_EVENTS counted it (alert_fatigue / rule_metric_kinds say NULL OR '').

Each test fails on e504d089; nothing opens a Snowflake session."""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.data import mart_sql
from app.logic.formulas import account_today

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="stub")


def _fail(kind: str = "timeout") -> QueryResult:
    return QueryResult(ok=False, error=f"boom ({kind})", error_kind=kind, source="stub")


# ---- R2-076: the prior-month storage tile never fabricates a zero ----------------------------------------------

def _storage(monkeypatch, prior: QueryResult, prior_live: QueryResult):
    from app.ui.pages.cost_parts import spend

    tib = 1024.0 ** 4
    mtd = _ok(pd.DataFrame({"DATABASE_NAME": ["DB_A"], "DB_BYTES": [2 * tib], "FAILSAFE_BYTES": [0.0],
                            "DAYS_AVERAGED": [10], "LATEST_DAY": [account_today() - timedelta(days=1)]}))

    def _run(_sql, *_a, key: str = "", **_k):
        return {"storage_mtd_ALL": mtd, "storage_prior_ALL": prior,
                "storage_prior_live_ALL": prior_live}.get(key, _fail())

    monkeypatch.setattr(spend, "run", _run)
    monkeypatch.setattr(spend, "_storage_table_drill", lambda *_a, **_k: None)
    monkeypatch.setattr(spend, "_account_storage_tiers", lambda *_a, **_k: None)
    monkeypatch.setattr(spend.charts, "bar_usd", lambda *_a, **_k: None)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _spend._storage_tab("ALL", 30, {}, bounds=None)

    at = AppTest.from_function(_app, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    tiles = " ".join(str(m.value) for m in at.markdown)
    errors = " | ".join(str(e.value) for e in at.error)
    return tiles, errors


def _prior_tile(tiles: str) -> str:
    m = re.search(r"Prior full month(.{0,600})", tiles, re.S)
    assert m, tiles
    return m.group(1)


def test_a_failed_prior_month_read_is_unavailable_not_zero(monkeypatch):
    tiles, errors = _storage(monkeypatch, _fail("timeout"), _fail("timeout"))
    tile = _prior_tile(tiles)
    assert "0.00 TiB" not in tile and "no prior data" not in tile      # pre-fix: '0.00 TiB' / 'no prior data'
    assert "—" in tile and "prior month unavailable" in tile
    assert "The prior full month's storage could not be read" in errors


def test_an_empty_prior_month_is_a_dash_with_no_prior_data(monkeypatch):
    tiles, errors = _storage(monkeypatch, _ok(pd.DataFrame()), _ok(pd.DataFrame()))
    tile = _prior_tile(tiles)
    assert "0.00 TiB" not in tile and "—" in tile and "no prior data" in tile
    assert "could not be read" not in errors


def test_a_read_prior_month_still_shows_its_size_and_mom(monkeypatch):
    tib = 1024.0 ** 4
    first_this = account_today().replace(day=1)
    pri = _ok(pd.DataFrame({"DATABASE_NAME": ["DB_A"], "DB_BYTES": [1 * tib], "FAILSAFE_BYTES": [0.0],
                            "DAYS_AVERAGED": [30], "LATEST_DAY": [first_this - timedelta(days=1)]}))
    tiles, _ = _storage(monkeypatch, pri, _fail())
    tile = _prior_tile(tiles)
    assert "1.00 TiB" in tile and "MoM" in tile


# ---- lead: no "open the waterfall above" when the explanation was refused --------------------------------------

def _left_edge_spike() -> QueryResult:
    """The spike is the OLDEST loaded day, so explain_by_warehouse has no prior history and refuses (R1-070)."""
    last = account_today() - timedelta(days=1)
    days = [last - timedelta(days=o) for o in range(21, -1, -1)]
    return _ok(pd.DataFrame({"DAY": days, "WAREHOUSE_NAME": "WH_A", "COMPANY": "ALFA",
                             "CREDITS_TOTAL": [300.0] + [33.4] * 21, "CREDITS_COMPUTE": [296.0] + [33.0] * 21}))


def test_a_refused_waterfall_is_explained_not_pointed_to(monkeypatch):
    from tests.test_cost_spend_honesty import _attribution

    at, _ = _attribution(monkeypatch, daily=_left_edge_spike())
    caps = " | ".join(str(c.value) for c in at.caption)
    assert "is a statistical outlier" in " ".join(str(w.value) for w in at.warning)     # the flag still fires
    assert "No root-cause waterfall for" in caps and "Not enough history before" in caps
    assert "open the waterfall above" not in caps                                     # pre-fix: always said
    assert "How to investigate a flag: jump to **Operations ▸ Queries**" in caps


def test_a_rendered_waterfall_keeps_the_open_it_step(monkeypatch):
    from tests.test_cost_spend_honesty import _attribution, _spiking_daily

    at, _ = _attribution(monkeypatch, daily=_spiking_daily())
    caps = " | ".join(str(c.value) for c in at.caption)
    assert "open the waterfall above" in caps and "No root-cause waterfall" not in caps


# ---- lead: fleet slow-key list excludes batch wall-clock rows --------------------------------------------------

def _count_if_db() -> sqlite3.Connection:
    class _CountIf:
        def __init__(self):
            self.n = 0

        def step(self, cond):
            self.n += 1 if cond else 0

        def finalize(self):
            return self.n

    c = sqlite3.connect(":memory:")
    c.create_aggregate("COUNT_IF", 1, _CountIf)
    return c


def test_fleet_query_stats_never_lists_a_batch_wall_row():
    sql = mart_sql.fleet_query_stats(7)
    assert "AND NOT STARTSWITH(QUERY_KEY, 'batch_wall:')" in sql
    assert "AND NOT STARTSWITH(QUERY_KEY, 'batch_wall:')" in mart_sql.fleet_query_stats(7, page="Cost Intelligence")


# ---- lead: rule_precision's UNTAGGED bucket is NULL or empty, like its siblings --------------------------------

def test_rule_precision_buckets_add_up_with_an_empty_string_kind():
    c = _count_if_db()
    c.execute("CREATE TABLE ALERT_EVENTS(RULE_ID TEXT, STATUS TEXT, RESOLUTION_KIND TEXT, RAISED_AT TEXT)")
    rows = [("R1", "RESOLVED", "ACTIONED"), ("R1", "RESOLVED", "NOISE"), ("R1", "RESOLVED", None),
            ("R1", "RESOLVED", ""), ("R1", "RESOLVED", "SUPERSEDED")]
    c.executemany("INSERT INTO ALERT_EVENTS VALUES (?,?,?, '2026-09-30')", rows)
    sql = mart_sql.rule_precision(90)
    sql = re.sub(r"\b[A-Z_]+\.[A-Z_]+\.(ALERT_EVENTS)\b", r"\1", sql)
    sql = sql.replace("DATEADD('day', -90, CURRENT_TIMESTAMP())", "'2026-07-01'")
    cur = c.execute(sql)
    row = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
    assert row["UNTAGGED"] == 2                                   # pre-fix: 1 (the '' kind fell out)
    assert row["RESOLVED_EVENTS"] == row["ACTIONED"] + row["NOISE"] + row["EXPECTED"] + row["UNTAGGED"]
    assert row["PRECISION_PCT"] == 50.0


def test_the_below_warehouse_drill_has_no_dead_none_branches():
    """Cleanup lead: run_batch returns every key, so the drill indexes its results directly."""
    from tests._source import read

    body = read("app/ui/pages/cost_parts/spend.py").split("def _below_warehouse_drill", 1)[1].split("\ndef ", 1)[0]
    assert 'xd, chg = _b["xdim"], _b["whchg"]' in body
    assert "is None" not in body.split('xd, chg = _b["xdim"], _b["whchg"]', 1)[1]
