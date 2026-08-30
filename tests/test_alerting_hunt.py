"""Alerting-layer bug hunt (v4.347.0) — locks for the app-side fixes.

The alerting hunt confirmed 8 defects; 3 are app-side (fixed here), 5 are
migration-bearing (owner-gated, tracked separately). App-side: two delivery-backlog
builders used a flat 24h window while the sender (V064) keeps CRITICAL eligible 7d,
and the MTTA/MTTR headline was a mean-of-weekly-means instead of event-weighted.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import mart_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# 2 + 6) delivery-backlog / eligibility mirror the sender's severity-aware window
def test_delivery_builders_mirror_the_7d_critical_send_window():
    for sql in (mart_sql.route_backlog(), mart_sql.last_delivery_health()):
        assert "e.SEVERITY = 'CRITICAL'" in sql
        assert "DATEADD('day', -7, CURRENT_TIMESTAMP())" in sql   # CRITICAL 7d
        assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" in sql  # non-CRITICAL 24h
        sqlglot.parse(sql, dialect="snowflake")
    # route_backlog no longer uses a bare flat-24h eligibility bound
    assert "e.RAISED_AT >= DATEADD('hour', -24" not in mart_sql.route_backlog()
    # both eligible-split branches + dist_eligible in last_delivery_health use the CASE
    assert mart_sql.last_delivery_health().count("CASE WHEN e.SEVERITY = 'CRITICAL'") >= 3


def test_send_eligible_window_constant_is_severity_aware():
    assert "CASE WHEN e.SEVERITY = 'CRITICAL'" in mart_sql._SEND_ELIGIBLE_SINCE
    assert "DATEADD('day', -7" in mart_sql._SEND_ELIGIBLE_SINCE
    assert "DATEADD('hour', -24" in mart_sql._SEND_ELIGIBLE_SINCE


# 4) MTTA/MTTR headline is event-weighted, not a mean of weekly means
def test_alert_mttr_exposes_the_event_weights():
    # the weighting needs per-week ACKED / RESOLVED counts from the builder
    sql = mart_sql.alert_mttr(90)
    assert "AS ACKED" in sql and "AS RESOLVED" in sql
    assert "AS MTTA_MIN" in sql and "AS MTTR_MIN" in sql


def test_mtta_mttr_headline_is_event_weighted_not_mean_of_means():
    a = _src("app/ui/pages/alerts.py")
    blk = a.split("def _weighted_minutes", 1)[1].split("kpi_row(", 1)[0]
    assert "(v * w).sum() / total" in blk            # weighted collapse
    assert "return float((v * w).sum() / total) if total > 0 else None" in blk
    # applied to both headlines with the right weight column
    assert '_weighted_minutes("MTTA_MIN", "ACKED")' in a
    assert '_weighted_minutes("MTTR_MIN", "RESOLVED")' in a
    # the old unweighted mean-of-weekly-means is gone
    assert 'latest["MTTA_MIN"].mean()' not in a
    # numeric proof of the weighting (replicates the inline helper on the finding's repro)
    df = pd.DataFrame({"ACKED": [1, 100], "RESOLVED": [1, 100],
                       "MTTA_MIN": [100.0, 10.0], "MTTR_MIN": [100.0, 10.0]})
    sub = df.dropna(subset=["MTTR_MIN"]).tail(4)
    w = pd.to_numeric(sub["RESOLVED"], errors="coerce").fillna(0.0)
    v = pd.to_numeric(sub["MTTR_MIN"], errors="coerce").fillna(0.0)
    weighted = float((v * w).sum() / float(w.sum()))
    assert abs(weighted - (1 * 100 + 100 * 10) / 101) < 0.01   # ~10.89, not the 55.0 mean-of-means
