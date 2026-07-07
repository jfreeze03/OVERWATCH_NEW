"""P0 polish contracts: no migration-speak in user pages, literal filters,
configurable score weights, HTML summary."""

from pathlib import Path

from app.core.sqlsafe import contains_filter
from app.logic import scoring
from app.logic.formulas import exec_summary_html

_PAGES = Path(__file__).resolve().parents[1] / "app" / "ui" / "pages"


def test_no_migration_speak_outside_admin():
    """CoCo P0: 'Run migration VX' is a developer message. Admin only."""
    offenders = []
    for p in _PAGES.glob("*.py"):
        if p.name == "admin.py":
            continue
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if "migration V" in line or "run V0" in line.lower() or "V00" in line or "V01" in line:
                offenders.append(f"{p.name}:{i}")
    assert not offenders, offenders


def test_contains_filter_matches_literally():
    clause = contains_filter("WAREHOUSE_NAME", "WH_TRXS")
    assert clause == "WAREHOUSE_NAME ILIKE '%WH~_TRXS%' ESCAPE '~'"
    assert "~%" in contains_filter("USER_NAME", "100%")
    # '~' can't reach the escaper: the whitelist strips it first (defense in depth)
    assert contains_filter("USER_NAME", "a~b") == "USER_NAME ILIKE '%ab%' ESCAPE '~'"


def test_score_weights_configurable_and_bounded():
    signals = {"critical_alerts": 2, "spill_gb": 15}
    default = scoring.platform_score(signals)
    heavier = scoring.platform_score(signals, weights={"SCORE_PTS_PER_CRITICAL": 10})
    assert heavier.score < default.score
    # caps still hold: absurd weight cannot zero the score from one driver
    capped = scoring.platform_score({"critical_alerts": 100},
                                    weights={"SCORE_PTS_PER_CRITICAL": 500})
    assert capped.score >= 76  # 100 - 24 cap
    # settings resolution: bad values fall back
    w = scoring.resolve_weights({"SCORE_PTS_PER_CRITICAL": "not-a-number"})
    assert w["SCORE_PTS_PER_CRITICAL"] == 6.0
    assert scoring.resolve_weights({"SCORE_PTS_PER_CRITICAL": "3"})["SCORE_PTS_PER_CRITICAL"] == 3.0


def test_exec_summary_html_self_contained():
    html = exec_summary_html(
        company="ALFA", days=30, generated="2026-07-07 12:00", window_spend="$1,234",
        mtd_line="$5,000 vs $50,000 budget", forecast_line="$48,000 ($45,000–$51,000)",
        alerts_line="1 critical · 2 high", score_line="88/100 (Healthy)",
        drivers=[("Critical alerts", "6.0", "1 open critical alerts.")],
        actions=["[HIGH] Fix loader — owner KEBARR1"],
    )
    assert html.startswith("<!DOCTYPE html>")
    assert "ALFA" in html and "88/100" in html and "Critical alerts" in html
    assert "<script" not in html.lower()  # static document, nothing executable


def test_gov_weights_configurable():
    from app.logic.governance import governance_drift, resolve_gov_weights

    base = governance_drift({"mfa_gap_users": 2})
    heavier = governance_drift({"mfa_gap_users": 2}, weights={"GOV_PTS_MFA_GAP": 10})
    assert heavier.score < base.score
    w = resolve_gov_weights({"GOV_PTS_MFA_GAP": "bad"})
    assert w["GOV_PTS_MFA_GAP"] == 5.0
    assert resolve_gov_weights({"GOV_PTS_EXPIRED_CRED": "4"})["GOV_PTS_EXPIRED_CRED"] == 4.0


def test_contract_exhaustion_reader_and_features_index():
    from pathlib import Path

    from app.data import mart_sql

    sql = mart_sql.contract_exhaustion()
    assert "CONTRACT_CREDITS" in sql and "DAYS_LEFT" in sql and "EXHAUST_DATE" in sql
    root = Path(__file__).resolve().parents[1]
    feats = (root / "FEATURES.md").read_text(encoding="utf-8")
    for marker in ("pre-explained", "Incident correlation timeline", "kill-switch",
                   "Renewal planner", "ML.FORECAST"):
        assert marker in feats, marker
    arch = (root / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Deliberate choices reviewers will ask about" in arch
    assert "Webhook delivery IS wired" in arch
