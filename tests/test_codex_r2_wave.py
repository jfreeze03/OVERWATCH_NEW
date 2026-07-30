"""Codex review round 2 — DO-FIRST wave locks (v4.89+).

Wave 1: A-score-1 (Overview score/KPI count from the UNCAPPED severity aggregate,
not the 500-row feed), A-score-3 (relabel the midnight-aligned window honestly),
rec 10 (score/task-node reads sit on the hourly tier matching their source cadence).
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A-score-1 — Overview counts criticals/highs from the uncapped aggregate
# ---------------------------------------------------------------------------
def test_ascore1_overview_score_uses_uncapped_aggregate():
    ov = _src("app/ui/pages/overview.py")
    # the score/KPI alert counts come from the uncapped COUNT_IF aggregate...
    assert "open_alert_severity_counts(company)" in ov
    assert 'key=f"alert_counts_{company}"' in ov
    # ...NOT the 500-row feed (which undercounts in a storm and would inflate the score)
    assert "open_alert_events(500" not in ov
    # counts read from the CRIT/HIGH columns of the aggregate row
    assert 'safe_float(_row.get("CRIT"))' in ov and 'safe_float(_row.get("HIGH"))' in ov


def test_ascore1_score_source_parity_with_alerts_page():
    # ADDED REC #1: the platform-score critical/high inputs and the Alerts-page KPI
    # must resolve through the SAME source (open_alert_severity_counts). This feed
    # diverged between the two surfaces once (C4/C7 fixed only Alerts); lock it so it
    # cannot silently regress a third time.
    ov, al = _src("app/ui/pages/overview.py"), _src("app/ui/pages/alerts.py")
    assert "open_alert_severity_counts(company)" in ov
    assert "open_alert_severity_counts(company)" in al


# ---------------------------------------------------------------------------
# A-score-3 — the score-health window is labelled honestly
# ---------------------------------------------------------------------------
def test_ascore3_window_relabelled():
    ov = _src("app/ui/pages/overview.py")
    assert "fixed 24h" not in ov                     # the misleading label is gone
    assert "prev + current calendar day" in ov       # the source labels are honest
    assert "previous + current calendar day" in ov   # the KPI help too


# ---------------------------------------------------------------------------
# rec 10 — cache tiers match source refresh cadence (hourly, not 5-min recent)
# ---------------------------------------------------------------------------
def test_rec10_score_reads_on_hourly_tier():
    ov = _src("app/ui/pages/overview.py")
    # the throughput + task score reads, and the retro score_inputs, refresh
    # hourly/daily at source, so they cache at the hourly tier (salt still forces refresh)
    thr = ov.split('key=f"score_throughput_{company}"', 1)[1].split(")", 1)[0]
    assert 'tier="hourly"' in thr
    tk = ov.split('key=f"score_tasks_{company}"', 1)[1].split(")", 1)[0]
    assert 'tier="hourly"' in tk
    assert 'mart_tier="hourly", live_tier="hourly"' in ov   # score_inputs run_mart_first


def test_rec10_task_node_panel_on_hourly_tier():
    ops = _src("app/ui/pages/operations.py")
    node = ops.split('key=f"t_node_{company}_{days}"', 1)[1].split(")", 1)[0]
    assert 'tier="hourly"' in node   # MART_TASK_NODE_DAILY loads hourly, not every 5 min


# ---------------------------------------------------------------------------
# rec 5 — executive downloads built from the honest screen view-model
# ---------------------------------------------------------------------------
def test_rec5_export_incomplete_and_scope_honest():
    ov = _src("app/ui/pages/overview.py")
    # an Incomplete score must NOT export as a real-looking 0/100
    assert '"Incomplete — health inputs unavailable"' in ov
    assert "_score_export" in ov
    # account-wide figures carry their scope; window spend is labelled company/metering
    assert "· account-wide" in ov
    assert "warehouse metering" in ov


def test_rec5_footer_distinguishes_billed_vs_window_spend():
    from app.logic.formulas import exec_summary_html
    html = exec_summary_html(
        company="ALFA", days=30, generated="2026-07-30 (account time)",
        window_spend="$1 · ALFA, metering", mtd_line="$5 · account-wide",
        forecast_line="$4 · account-wide", alerts_line="0 critical",
        score_line="Incomplete — health inputs unavailable", drivers=[], actions=[])
    # the footer no longer blanket-claims the cloud-services adjustment for ALL numbers
    assert "cloud-services adjustment applied; telemetry" not in html
    assert "window spend is warehouse metering" in html
    # the Incomplete score renders as honest text, not a fake 0/100
    assert "Incomplete" in html and "0/100" not in html


# ---------------------------------------------------------------------------
# A-score-2 — every penalty-bearing score input fails closed on a genuine outage
# ---------------------------------------------------------------------------
class _FakeRes:
    def __init__(self, ok, error_kind=""):
        self.ok, self.error_kind = ok, error_kind


def test_ascore2_degraded_sources_golden_matrix():
    # ADDED REC #5: outage kinds (timeout/unknown_function/other) fail closed; an
    # 'absent' mart (not installed) and an ok read stay zero-penalty. This is the
    # golden matrix that keeps a freshly-provisioned deployment from oscillating
    # between Incomplete and a falsely-healthy score.
    from app.logic.scoring import degraded_sources
    res = degraded_sources({
        "task": _FakeRes(False, "timeout"),
        "freshness": _FakeRes(False, "unknown_function"),
        "owner-queue": _FakeRes(False, "other"),
        "installed-ok": _FakeRes(True, ""),
        "not-installed": _FakeRes(False, "absent"),
    })
    assert res == {"task", "freshness", "owner-queue"}


def test_ascore2_platform_score_incomplete_on_degraded():
    from app.logic.scoring import platform_score
    # both REQUIRED present, but a degraded penalty source -> Incomplete (fail closed)
    r = platform_score(signals={}, available={"throughput", "alerts"}, degraded={"task"})
    assert r.state == "Incomplete" and r.score == 0
    assert "task" in r.drivers[0].evidence
    # empty degraded + full required -> scores normally (no regression)
    ok = platform_score(signals={}, available={"throughput", "alerts"}, degraded=set())
    assert ok.state == "Healthy"
    # backward-compat: degraded defaults to None (older callers unaffected)
    assert platform_score(signals={"critical_alerts": 1}).state != "Incomplete"


def test_ascore2_overview_wires_degraded_by_error_kind():
    ov = _src("app/ui/pages/overview.py")
    assert "scoring.degraded_sources(" in ov
    assert "degraded=_degraded" in ov
    # the budget special-case only fails closed on a genuine outage, not an absent mart
    assert 'getattr(_bt_hist, "error_kind", "") != "absent"' in ov
