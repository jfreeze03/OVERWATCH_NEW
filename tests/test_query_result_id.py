"""Next-Fifty #46(d): QueryResult.query_id carries the Snowflake query id of a run() cache MISS (the same
id the telemetry row records), '' on a cache hit, a failure, or a batch result — the measured ledger
verify stamps it into PROOF_QUERY_ID."""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

import app.core.query as q
from app.core.result import QueryResult


@pytest.fixture
def _quiet(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(q, "_telemetry", lambda *a, **kw: seen.append(kw))
    monkeypatch.setattr(q, "record_error", lambda *a, **k: None)
    return seen


def test_query_id_is_the_last_field_with_a_blank_default():
    fields = dataclasses.fields(QueryResult)
    assert fields[-1].name == "query_id" and fields[-1].default == ""
    assert QueryResult().query_id == ""


def test_a_miss_carries_the_query_id_and_a_hit_does_not(monkeypatch, _quiet):
    df = pd.DataFrame({"A": [1]})

    def _miss(sql, scope, page):
        q._FETCH_MISS.set(True)
        q._LAST_QUERY_ID.set("01ab-miss")
        return df

    monkeypatch.setitem(q._FETCHERS, "recent", _miss)
    res = q.run("SELECT 1 AS A", page="T", key="k", tier="recent")
    assert res.ok and not res.cache_hit and res.query_id == "01ab-miss"
    assert _quiet[-1]["query_id"] == "01ab-miss"                 # the telemetry row joins on the same id

    def _hit(sql, scope, page):                                  # a cache hit never runs the fetch body
        return df

    q._LAST_QUERY_ID.set("stale-id")
    monkeypatch.setitem(q._FETCHERS, "recent", _hit)
    hit = q.run("SELECT 1 AS A", page="T", key="k", tier="recent")
    assert hit.cache_hit and hit.query_id == "" and _quiet[-1]["query_id"] == ""


def test_a_failure_and_a_batch_result_carry_no_query_id(monkeypatch, _quiet):
    def _boom(sql, scope, page):
        raise RuntimeError("nope")

    monkeypatch.setitem(q._FETCHERS, "recent", _boom)
    assert q.run("SELECT 1", page="T", key="k", tier="recent").query_id == ""
    df = pd.DataFrame({"A": [1]})
    monkeypatch.setattr(q, "_BATCH_FETCHERS", {"recent": lambda sqls, scope, page: (df,)})
    out = q.run_batch([{"key": "b", "sql": "SELECT 1 AS A"}], page="T", tier="recent")
    assert out["b"].ok and out["b"].query_id == ""
