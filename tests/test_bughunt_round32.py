"""Bug-hunt round 32: cost attribution & chargeback money-math.

Low yield (1 confirmed / 2; the rate/rebate finder errored, two dimensions came back empty) — a
floor signal for this well-worked surface (V126 pipeline WH_CREDITS, V127 idle credits, round-28
billed empty-vs-zero already closed the big ones).

#1 (MED, WLA-1): the "Serverless tasks (billed separately)" table ignored the 'Last month' bounded
   scope. serverless_task_daily hard-coded a trailing START_TIME >= DATEADD('day', -days,
   CURRENT_DATE()) and the caller omitted bounds, while the pipeline-cost KPIs directly above it in
   the SAME panel honor bounds via scope_window_where. So under Last month the serverless dollars
   covered a rolling ~month ending today (session-tz) instead of the account-clock-bounded prior
   month — a wrong serverless total, inconsistent with the pipeline costs beside it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.data import graph_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_serverless_task_daily_honors_last_month_bounds():
    trailing = graph_sql.serverless_task_daily(30)
    bounded = graph_sql.serverless_task_daily(31, bounds=(date(2026, 8, 1), date(2026, 9, 1)))
    # a bounded read emits the explicit account-clock calendar range; trailing does not
    assert "2026-08-01" in bounded
    assert bounded != trailing
    # the builder now windows through scope_window_where (bounds-aware), not the hard-coded trailing
    body = _read("app/data/graph_sql.py").split("def serverless_task_daily", 1)[1].split("\ndef ", 1)[0]
    assert 'scope_window_where("START_TIME", days, bounds=bounds)' in body
    assert "START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())" not in body
    # the caller threads bounds AND the _lm cache-key suffix (so trailing vs Last-month don't collide)
    caller = _read("app/ui/pages/cost_parts/unit_costs.py")
    assert "serverless_task_daily(days, company, database, schema_contains, bounds=bounds)" in caller
    assert 'key=f"sls_costs_{company}_{days}_{database}_{schema_contains}{_lm}"' in caller
