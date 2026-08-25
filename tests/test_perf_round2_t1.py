"""Locks for perf round 2 T1 quick wins (docs/reviews/PERF_ROUND_2_SCOPE_2026-07-29.md).
Scope of this pass: T1.1 (hourly tier on Overview + Cost/Spend facts), T1.6 (CSV
prep keying), T1.9 (open_alert_events LIMIT). T1.11 was declined (audit rule).
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- T1.1: hourly tier on hourly-loaded fact/mart reads ----------------------

def test_t1_1_overview_facts_on_hourly_tier():
    ov = _read("app/ui/pages/overview.py")
    for key in ('key=f"exec_board_{company}_{window or days}", tier="hourly"',
                'key="spark_activity", tier="hourly"',
                'key="daily_digest", tier="hourly"'):
        assert key in ov, key
    # PERF #46: the 45d + 150d FACT_METERING_DAILY reads moved to the shared daily_spend_wide()
    # accessor (one cache entry across pages); it is STILL on the hourly tier, and Overview
    # routes through it.
    assert "daily_spend_wide(_PAGE)" in ov
    cmp_src = _read("app/ui/components.py")
    assert "def daily_spend_wide(" in cmp_src
    assert 'key="fact_daily_wide", tier="hourly"' in cmp_src
    assert "mart_sql.exec_board(company, days, window)" in ov
    # rec 10 (v4.89): score-inputs moved recent -> hourly too — FACT_PLATFORM_SCORE_DAILY
    # / the retro facts refresh daily, so the 5-min tier only re-paid unchanged reads.
    assert 'mart_tier="hourly", live_tier="hourly"' in ov


def test_t1_1_spend_facts_on_hourly_tier():
    sp = _read("app/ui/pages/cost_parts/spend.py")
    for key in ('key=f"csr_fact_{company}_{days}", tier="hourly"',
                'key=f"cs_shapes_{company}_{days}_{pick}", tier="hourly"',
                'key=f"cs_users_{company}_{days}_{pick}", tier="hourly"',
                'key=f"wh_vs_prior_fact_{company}_{days}", tier="hourly"',
                'key=f"fact_wh_daily_{company}", tier="hourly"',
                'key=f"stor_acct_{days}", tier="hourly"'):
        assert key in sp, key
    # the live WMH fallback stays off the hourly tier
    assert 'key=f"cs_ratio_{company}_{days}", tier="recent"' in sp


def test_t1_1_cost_batch_and_unmapped_on_hourly_tier():
    cost = _read("app/ui/pages/cost.py")
    assert 'run_batch(_spend_attr_recent_jobs(f["company"], f["days"]),' in cost
    assert 'page=_PAGE, tier="hourly") or {}' in cost
    assert 'key=f"unmapped_{f[\'days\']}", tier="hourly"' in cost


# --- T1.6: CSV prep keyed by page + content, single evicting slot -------------

def test_t1_6_csv_prep_is_page_and_content_keyed():
    cmp = _read("app/ui/components.py")
    assert 'st.session_state["_ow_dl_page"] = str(title)' in cmp      # page identity stamped
    assert "def _download_fingerprint(df)" in cmp
    assert 'st.session_state["_ow_dlprep"] = {' in cmp                # single slot, dict
    assert '_slot.get("fp") == _fp' in cmp                            # served only on match
    assert "ow_dlprep_{key or ''}_{seq}" not in cmp                   # positional key gone


# --- T1.4: change-impact drill scans gated behind selection/toggle -----------

def test_t1_4_change_impact_drills_are_gated():
    op = _read("app/ui/pages/operations.py")
    # warehouse 28d history runs only on an explicit row selection, historical tier
    assert ("if sel is not None and row is not None:\n"
            "            hist = run(change_impact_sql.warehouse_daily_series") in op
    assert ("row = df.iloc[int(sel)] if sel is not None and 0 <= int(sel) < len(df) "
            "else None") in op
    assert 'key=f"whchg_hist_{row[\'WAREHOUSE_NAME\']}", tier="historical"' in op
    # object 28d history behind a row click or a load toggle, historical tier
    assert 'st.toggle("Load 28-day run history", key="chg_hist_toggle")' in op
    assert 'key=f"chg_hist_{pick}", tier="historical"' in op
    # the old unconditional-scan tiers are gone
    assert 'key=f"whchg_hist_{row[\'WAREHOUSE_NAME\']}", tier="recent"' not in op
    assert 'key=f"chg_hist_{pick}", tier="recent"' not in op


# --- T2.1: Control Room live trio batched; proposals gated to operators ------

def test_t2_1_control_room_live_trio_batched():
    cr = _read("app/ui/pages/control_room.py")
    assert 'run_batch(_live_specs, page=_PAGE, tier="live")' in cr        # one live round trip
    for m in ('"key": "oi"', '"key": "cra"', '"key": "props"'):
        assert m in cr, m
    # each read is prefetch-else-run (batch member or its own serial fallback)
    assert '_live_pf.get("oi") or run(' in cr
    assert '_live_pf.get("cra") or run(' in cr
    # proposals only fetched for operators (non-operators stop paying for it).
    # rec1 moved this batch into the Incidents & triage section (deeper indent),
    # so match whitespace-insensitively — the coupling (append inside if _is_op) holds.
    assert "if _is_op: _live_specs.append(" in " ".join(cr.split())
    assert '_live_pf.get("props") or run(' in cr
    assert ") if _is_op else None" in cr


def test_t2_3_run_mart_first_has_preloaded_seam():
    comp = _read("app/ui/components.py")
    fn = comp.split("def run_mart_first(", 1)[1].split("\ndef ", 1)[0]
    assert "preloaded=None" in fn
    assert "preloaded if (preloaded is not None and preloaded.ok) else run(" in fn


# --- T1.9: open_alert_events standardized at LIMIT 500 -----------------------

def test_t1_9_open_alert_events_limit_standardized():
    # A-score-1 (v4.89): Overview no longer fetches the 500-row feed — it renders no
    # alert LIST, so it counts criticals/highs from the uncapped severity aggregate
    # (open_alert_severity_counts). Alerts + Control Room keep the feed for their lists.
    assert 'mart_sql.open_alert_events(500, company)' not in _read("app/ui/pages/overview.py")
    assert 'mart_sql.open_alert_severity_counts(company)' in _read("app/ui/pages/overview.py")
    assert 'mart_sql.open_alert_events(500, company)' in _read("app/ui/pages/alerts.py")
    assert 'mart_sql.open_alert_events(500, company)' in _read("app/ui/pages/control_room.py")
    # no stale caption claiming the old cap
    assert "300 most recent" not in _read("app/ui/pages/alerts.py")
