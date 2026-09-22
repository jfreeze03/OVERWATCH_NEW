"""Locks for the v4.574.0 UX pass (Themes A & B): color/signal honesty + error-state consistency.

Restores documented conventions — a section stripe carries state (alarm_health), green never means
"nothing loaded"/"broken read", and a raw Snowflake error leads with one line + a detail expander.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_security_headers_are_not_hardcoded_warn():
    sec = _read("app/ui/pages/security.py")
    # the eager panels derive the stripe from their data; toggle-gated ones are neutral until scanned
    assert 'alarm_health(nn), "alerts"' in sec           # new networks
    assert "alarm_health(creds), " in sec                # expiring credentials
    assert "alarm_health(bga), " in sec                  # break-glass (should hug zero)
    assert 'Account-takeover candidates (failed burst → success)", ""' in sec
    assert 'Dormant accounts that just woke up (long-gap logins)", ""' in sec
    assert 'AI usage behavior (Cortex Code)", ""' in sec
    # the six reviewer-verified false-alarm headers no longer hardcode "warn" (other "warn"
    # headers on this page — e.g. revocation-impact, exposure — are data-populated, not false
    # all-clears, so they are intentionally left alone).
    assert '(failed burst → success)", "warn"' not in sec
    assert 'privileged users (90-day baseline)", "warn"' not in sec
    assert 'Expiring credentials (10-day horizon)", "warn"' not in sec
    assert 'just woke up (long-gap logins)", "warn"' not in sec
    assert 'AI usage behavior (Cortex Code)", "warn"' not in sec
    assert 'should hug zero)", "warn"' not in sec
    assert '", "warn", "admin", anchor="sec-admin-grants"' not in _read("app/ui/security_center.py")


def test_overview_alerts_kpi_not_green_on_failed_read():
    ov = _read("app/ui/pages/overview.py")
    assert 'if alerts_res.ok else "Unavailable"' in ov
    assert '"severity": ("warn" if not alerts_res.ok else' in ov
    # the old fall-through-to-ok ternary is gone
    assert '"warn" if (alerts_res.ok and high_alerts) else "ok")' not in ov


def test_etl_panels_dont_use_clean_for_nothing_here():
    ops = _read("app/ui/pages/operations.py")
    assert 'empty_state("no_data_yet", f"No reference-gap checks configured for {database} ' in ops
    assert 'empty_state("no_data_yet", f"No ETL runs recorded{_scope}.' in ops


def test_delivery_health_card_discloses_failed_read():
    al = _read("app/ui/pages/alerts.py")
    assert 'empty_state("unavailable", "Delivery health unavailable' in al


def test_ask_answer_is_neutral_not_green_success():
    ask = _read("app/ui/pages/ask.py")
    assert "st.success(result.headline)" not in ask
    assert "with st.container(border=True):" in ask


def test_raw_errors_use_the_unavailable_idiom():
    # a representative sample across the 9 converted sites — none dump {X.error} into st.error/st.warning
    cr = _read("app/ui/pages/control_room.py")
    assert 'empty_state("unavailable", "Pulse unavailable.", detail=pulse.error)' in cr
    assert 'empty_state("unavailable", "Activity trend unavailable.", detail=act.error)' in cr
    assert 'empty_state("unavailable", "Failure detail unavailable.", detail=res.error)' in _read("app/ui/pages/operations.py")
    assert 'empty_state("unavailable", "Spend history unavailable.", detail=trend_source.error)' in _read("app/ui/pages/overview.py")
    adm = _read("app/ui/pages/admin.py")
    assert 'empty_state("unavailable", "No Snowflake session.", detail=ctx.error)' in adm
    assert 'empty_state("unavailable", "Cannot read SCHEMA_VERSION.", detail=res.error)' in adm
    assert "detail=res.error,   # r-ux" in _read("app/ui/pages/cost_parts/contract.py")
    assert 'empty_state("unavailable", "Month credits could not be read.", detail=month_res.error)' in _read("app/ui/pages/cost_parts/ai_chargeback.py")
