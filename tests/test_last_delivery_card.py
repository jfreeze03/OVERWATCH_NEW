"""Locks for the 'Last alert delivery' card + the integration-probe fix.

Both come from a real incident (2026-07-31): the owner saw no Teams alerts for a day and
could not tell a healthy quiet stretch from a dead pipe. Two things were wrong —
(1) the app HAD the last-send timestamp but buried it as a suffix on a banner, and
(2) the banner probed a HARDCODED 'OVERWATCH_WEBHOOK' (the Slack placeholder), which on a
Teams-only account does not exist — so it rendered a red "No webhook integration" while
Teams was delivering fine.

The design rule these pin: SILENCE IS NOT A FAULT SIGNAL. The sender is a 24h-windowed,
per-key digest, so a chronic condition raises once and quiet days are legitimately empty.
Quiet is separated from broken by what is ELIGIBLE, not by the absence of messages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# #31 (Codex): the floor-compat CI job does not install sqlglot, but this module
# imported it at top level — so a clean floor runner failed at COLLECTION, not just
# on this test. importorskip lets the module load there and skips only the parse
# assertion; where sqlglot is installed (the main suite) it runs unchanged.
sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]
_ALERTS = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")


def test_builder_parses_and_returns_the_four_signals():
    from app.data import mart_sql
    sql = mart_sql.last_delivery_health()
    sqlglot.parse(sql, dialect="snowflake")
    for col in ("LAST_SENT_AT", "MINUTES_SINCE", "ELIGIBLE_NOW",
                "ROUTE_FAILS_24H", "ENABLED_ROUTES"):
        assert col in sql, col


def test_eligibility_mirrors_the_senders_own_predicate():
    """ELIGIBLE_NOW is only meaningful if it matches what the sender would actually pick
    up — otherwise the card's quiet-vs-broken verdict is guesswork."""
    from app.data import mart_sql
    sql = mart_sql.last_delivery_health()
    assert "e.STATUS = 'OPEN'" in sql
    assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" in sql        # the 24h send window
    assert "JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG" in sql         # the INNER config join
    assert "r.FAMILY = 'ALL' OR c.FAMILY = r.FAMILY" in sql          # route family
    assert "r.MIN_SEVERITY" in sql                                   # route severity floor
    assert "COMPANY_FILTER" in sql                                   # route company scope
    assert "NOT EXISTS" in sql and "ALERT_DELIVERIES d" in sql       # per-route ledger


def test_last_send_reads_the_delivery_ledger_not_raises():
    # a confirmed SEND, not a raise: NOTIFIED_AT can be stamped without Teams showing it
    from app.data import mart_sql
    sql = mart_sql.last_delivery_health()
    assert "MAX(SENT_AT) AS LAST_SENT_AT" in sql
    assert "FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES" in sql


def test_send_failures_are_counted_separately_from_silence():
    from app.data import mart_sql
    sql = mart_sql.last_delivery_health()
    assert "'route_send_failed'" in sql and "'NotifyWebhook'" in sql


def test_card_is_rendered_under_the_delivery_banner():
    # compare CALL sites (indented), not definitions — the card is defined above the
    # banner but must render below it
    assert "\n    _last_delivery_card()\n" in _ALERTS
    assert _ALERTS.index("\n    _delivery_status()\n") < _ALERTS.index("\n    _last_delivery_card()\n")


def test_card_separates_quiet_from_broken():
    card = _ALERTS.split("def _last_delivery_card", 1)[1].split("\ndef ", 1)[0]
    # the three verdicts that make the card worth having
    assert "delivery looks stuck" in card            # waiting + nothing sent
    assert "the endpoint is refusing" in card        # route_send_failed present
    assert "no news here really is no news" in card  # nothing waiting -> quiet is CORRECT
    # and it must never call plain silence a fault
    assert "Silence alone is not a fault signal" in card


def test_builder_is_per_route_not_global():
    """Codex #29: one row per enabled route so a healthy route can't mask a dead sibling
    and one dead route can't redden the whole card."""
    from app.data import mart_sql
    sql = mart_sql.last_delivery_health()
    sqlglot.parse(sql, dialect="snowflake")
    # per-route grain columns
    for col in ("ROUTE_ID", "INTEGRATION_NAME", "FAILING_NOW", "STUCK", "CONSEC_FAILS"):
        assert col in sql, col
    # last send is per route (grouped), not one global MAX
    assert "SELECT ROUTE_ID, MAX(SENT_AT) AS LAST_SENT_AT" in sql
    assert "GROUP BY ROUTE_ID" in sql
    # failures are attributed per route from the sender's CONTEXT string
    assert "SPLIT_PART(CONTEXT, ' ', 2)" in sql
    # always >=1 row: a synthetic zero-route row keeps the 'nothing can be delivered' state
    assert "sm.ENABLED_ROUTES = 0" in sql


def test_failing_now_is_recovery_aware():
    """Codex #42: red ONLY when the LATEST failure is newer than the LATEST success —
    a later success clears the red, so a recovered endpoint does not stay red 24h."""
    from app.data import mart_sql
    sql = mart_sql.last_delivery_health()
    assert "f.LAST_FAILURE_AT IS NOT NULL" in sql
    assert "f.LAST_FAILURE_AT > s.LAST_SENT_AT" in sql          # latest fail beats latest success
    assert "AS FAILING_NOW" in sql
    # consecutive failures are counted only AFTER the last success
    assert "a.LOGGED_AT > COALESCE(s.LAST_SENT_AT" in sql


def test_stuck_fires_on_never_sent_backlog():
    """Codex #41: a route with an eligible backlog that has NEVER sent (or is silent
    >=90m) is BROKEN immediately, not 'queued for the next run'."""
    from app.data import mart_sql
    sql = mart_sql.last_delivery_health()
    assert "s.LAST_SENT_AT IS NULL" in sql                      # never-sent arm
    assert ">= 90) AS STUCK" in sql or "DATEDIFF('minute', s.LAST_SENT_AT, CURRENT_TIMESTAMP()) >= 90" in sql


def test_card_aggregates_worst_route_and_names_it():
    """The card ranks failing > stuck > waiting > quiet and names the offending route."""
    card = _ALERTS.split("def _last_delivery_card", 1)[1].split("\ndef ", 1)[0]
    assert "df[df[\"ROUTE_ID\"].notna()]" in card               # per-route rows
    assert "_flag(rdf[\"FAILING_NOW\"])" in card                # worst-first: failing routes
    assert "_flag(rdf[\"STUCK\"])" in card                      # then stuck routes
    assert "_rlabel" in card and "is failing" in card           # names the route
    # #41: the old "queued for next run" no longer wins over a never-sent backlog —
    # STUCK is evaluated before the waiting branch
    assert card.index("not stuck.empty") < card.index("queued for the next run")
    """The bug that showed a false red on a Teams-only account."""
    assert "SHOW NOTIFICATION INTEGRATIONS LIKE 'OVERWATCH_WEBHOOK'" not in _ALERTS
    banner = _ALERTS.split("def _delivery_status", 1)[1].split("\ndef ", 1)[0]
    assert "core_object('ALERT_ROUTES')" in banner                # resolve from the routes
    assert "WHERE ENABLED" in banner
    assert "has_integ = bool(want & have) if want else bool(have)" in banner
    # a route naming a missing integration is surfaced, not silently ignored
    assert "missing = sorted(want - have)" in banner
    assert "does not exist" in banner
