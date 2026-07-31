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


# ---------------------------------------------------------------------------
# rec 20 — contract runway: canonical trailing-30-COMPLETE-days burn
# ---------------------------------------------------------------------------
def test_rec20_contract_exhaustion_complete_days_only():
    from app.data import mart_sql
    sql = mart_sql.contract_exhaustion()
    # trailing 30 COMPLETE days (today's partial excluded), divided by actual count
    assert "DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())" in sql
    assert "DATEADD('day', -1, CURRENT_DATE())" in sql
    assert "NULLIF(COUNT(DISTINCT DAY), 0)" in sql
    # the old partial-day-inclusive "/ 30" form is gone
    assert "SUM(CREDITS_BILLED), 0) / 30 FROM" not in sql
    # rec #2: the self-referential "same math as the alert block" drift comment is gone
    assert "Same math as the COST_CONTRACT_BREACH" not in sql


# ---------------------------------------------------------------------------
# rec 4 / added rec #3 — one effective window shared by the pool + every share
# ---------------------------------------------------------------------------
def test_rec3_resolve_effective_window():
    from app.config import MAX_MART_WINDOW_DAYS
    from app.data.common import resolve_effective_window
    # clamps to the vs-prior half-window cap and excludes today (half-open)
    eff, frag = resolve_effective_window(365)
    assert eff == MAX_MART_WINDOW_DAYS // 2   # 365 -> 182
    assert "< CURRENT_DATE()" in frag
    assert "DATEADD('day', -182, CURRENT_DATE())" in frag
    # below the cap passes through; column + max_days override honored
    eff2, frag2 = resolve_effective_window(7, "x.DAY", max_days=90)
    assert eff2 == 7 and "x.DAY >= DATEADD('day', -7, CURRENT_DATE())" in frag2


def test_rec4_pool_and_shares_share_one_window():
    from app.data import mart27_sql, mart_sql
    # the dollar POOL and the allocation SHARE denominator resolve through the SAME
    # helper: a share can no longer span 365d-with-today while the pool spans
    # 182d-without-today, so per-entity ALLOCATED_USD reconciles to the pool.
    pool = mart_sql.fact_warehouse_window_vs_prior(365, "ALFA")
    share = mart27_sql.alloc_xdim_attribution(365, "USER", "ALFA")
    for sql in (pool, share):
        assert "DATEADD('day', -182, CURRENT_DATE())" in sql   # same clamped window
        assert "< CURRENT_DATE()" in sql                        # both exclude today
    # the live fallback denominator excludes today + shares the contract (90-day cap)
    live = _src("app/data/cost_sql.py")
    assert 'resolve_effective_window(days, "START_TIME", max_days=90)' in live


# ---------------------------------------------------------------------------
# NEXT tier wave A — rec 9 (batch score reads) + rec 12 (dedup allocation chart)
# ---------------------------------------------------------------------------
def test_rec9_score_health_reads_batched():
    ov = _src("app/ui/pages/overview.py")
    assert "_score_pf = run_batch(" in ov
    assert '"key": f"score_throughput_{company}"' in ov and '"key": f"score_tasks_{company}"' in ov
    # both consumers pull from the batch (member-level fallback preserved)
    assert '_score_pf.get(f"score_throughput_{company}")' in ov
    assert '_score_pf.get(f"score_tasks_{company}")' in ov


def test_rec12_single_allocation_bar_with_other_row():
    sp = _src("app/ui/pages/cost_parts/spend.py")
    assert "charts.waterfall_usd(alloc" not in sp   # the duplicate cumulative waterfall is gone
    assert '"Other / not shown"' in sp              # explicit remainder row so the bar sums to 100%
    assert "top_n=11" in sp


# ---------------------------------------------------------------------------
# NEXT tier wave B — rec 13 (distinct card trust tokens) + rec 11 (section scope)
# ---------------------------------------------------------------------------
def test_rec13_card_renders_three_distinct_trust_tokens():
    from app.ui.components import metric_card_html
    # freshness, method and scope each render as their OWN chip — no single slot
    # where scope and freshness compete (the C11 collision this fixes).
    h = metric_card_html({"label": "MTD spend", "value": "$1",
                          "badge": "mart", "method": "billed", "scope": "account-wide"})
    assert "ow-src-badge--mart" in h        # freshness token
    assert "ow-src-badge--method" in h      # provenance token
    assert "ow-src-badge--scope" in h       # scope token
    assert h.count("ow-src-badge--") == 3   # exactly three chips, side by side
    # an unknown freshness value degrades to 'other', method/scope are free text
    h2 = metric_card_html({"label": "x", "value": "1", "badge": "weird", "scope": "company"})
    assert "ow-src-badge--other" in h2 and "ow-src-badge--scope" in h2


def test_rec13_theme_defines_method_and_scope_chip_styles():
    theme = _src("app/theme.py")
    assert ".ow-src-badge--method" in theme
    assert ".ow-src-badge--scope" in theme


def test_rec13_overview_billed_kpis_carry_method_and_scope():
    ov = _src("app/ui/pages/overview.py")
    # account-wide billed KPIs name BOTH how (billed) and scope (account-wide);
    # the company window-spend names metering + company. Distinct, never colliding.
    assert ov.count('"scope": "account-wide"') >= 5
    assert ov.count('"method": "billed"') >= 5
    assert '"method": "metering", "scope": "company"' in ov


def test_rec11_section_scope_note_only_fires_on_ignored_filters():
    from app.ui.components import section_scope_note
    # no dimension chip set -> no note (zero clutter on the common path)
    assert section_scope_note({"company": "ALFA", "days": 30}) == ""
    # an active warehouse chip the section ignores -> honest one-liner naming it
    note = section_scope_note({"warehouse_contains": "WH_ALFA_ADMIN"})
    assert "warehouse" in note and "ignore" in note.lower()
    # a chip the section DOES honor is not reported as ignored
    assert section_scope_note({"warehouse_contains": "x"}, honored=("warehouse_contains",)) == ""
    # multiple ignored chips are pluralized
    multi = section_scope_note({"warehouse_contains": "a", "user_contains": "b"})
    assert "filters" in multi and "warehouse" in multi and "user" in multi


def test_rec11_overview_renders_section_scope_note():
    ov = _src("app/ui/pages/overview.py")
    assert "section_scope_note(f)" in ov   # wired on the Overview headline KPIs
