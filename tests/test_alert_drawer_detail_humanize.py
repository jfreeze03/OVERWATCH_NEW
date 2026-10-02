"""R1-124 (alerts cluster): the Alerts drawer shows a change-regression event's DETAIL in Hr/Min/Sec.

SP_WAREHOUSE_CHANGE_SCAN (V109) and SP_CHANGE_IMPACT_SCAN (V140) copy VERDICT_DETAIL into ALERT_EVENTS.DETAIL with raw
seconds ('p95 1800.0s->2400.0s | queue 145.00->200.00 min/d'). The app already humanizes VERDICT_DETAIL on its four
read surfaces (wh_change.humanize_verdict_detail, v4.606); the drawer printed DETAIL verbatim, so a historic
WH_CHANGE_REGRESSION / PERF_CHANGE_REGRESSION row read '1800.0s' next to a KPI showing '30m' (owner rule: every
duration humanizes to Hr/Min/Sec). The shim is a no-op on text a later scan already humanized in SQL, so no schema
gate is needed. Other rules' DETAIL is never rewritten (it is evidence text).

Integration (v4.609): the shim writes the scans' own spaced ASCII ' -> ' arrow (detection review r1, V172), so a
pre-V172 event re-rendered in the drawer reads exactly like a V172 event (both in Hr/Min/Sec, one arrow) -- the
Unicode arrow the alerts cluster first pinned here would have shown two arrow styles side by side.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.ui.pages import alerts

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
_RAW_WH = "credits/day 10.5->12.25 | p95 1800.0s->2400.0s | queue 145.00->200.00 min/d | fail 0.0->1.5% | 120->140 queries"
_RAW_PERF = "Schema ALFA_DB.RAW changed (ALTER PROCEDURE) | p95 95.5s->120.0s | 40->44 calls"


@pytest.mark.parametrize("rule", ["WH_CHANGE_REGRESSION", "PERF_CHANGE_REGRESSION", " wh_change_regression "])
def test_change_regression_detail_reads_in_hr_min_sec(rule):
    got = alerts._drawer_detail(rule, _RAW_WH)
    assert "p95 30m -> 40m" in got and "queue 2h 25m -> 3h 20m/day" in got
    assert "\u2192" not in got and "\u2192" not in alerts._drawer_detail(rule, _RAW_PERF)   # the scans' ASCII arrow only
    assert "1800.0s" not in got and "min/d" not in got
    assert got.startswith("credits/day 10.5->12.25 | ") and got.endswith(" | fail 0.0->1.5% | 120->140 queries")
    perf = alerts._drawer_detail(rule, _RAW_PERF)
    assert "p95 1m 36s -> 2m" in perf and "95.5s" not in perf
    # a re-rendered pre-V172 event is a fixed point: the drawer renders it again unchanged, like V172's own text
    assert alerts._drawer_detail(rule, got) == got and alerts._drawer_detail(rule, perf) == perf


def test_other_rules_keep_their_detail_verbatim():
    for rule in ("PERF_QUEUED_MINUTES", "COST_IDLE_OPPORTUNITY", "", None):
        assert alerts._drawer_detail(rule, _RAW_WH) == _RAW_WH


def test_sql_humanized_text_passes_through_unchanged():
    """A scan that already humanizes in SQL (spaced ASCII arrow) matches neither regex: the shim is a no-op."""
    done = "credits/day 10.5->12.25 | p95 30m -> 40m | queue 2h 25m -> 3h 20m/day | fail 0->1.5%"
    assert alerts._drawer_detail("WH_CHANGE_REGRESSION", done) == done
    assert alerts._drawer_detail("WH_CHANGE_REGRESSION", "") == ""


def test_the_drawer_and_its_case_summary_use_the_humanized_text():
    block = _SRC[_SRC.index("                    detail_text = "):_SRC.index("key=f\"ow_case_add_alert_{event_id[:8]}\")")]
    assert block.startswith("                    detail_text = _drawer_detail(str(row[\"RULE_ID\"]), "
                            "str(row.get(\"DETAIL\") or \"\").strip())")
    assert "st.text(detail_text)" in block and "summary=(detail_text[:200] if detail_text" in block
    assert len(re.findall(r"_drawer_detail\(", _SRC)) == 2                       # the def + the one drawer call


def test_r2_020_window_hours_caption_says_widening_no_longer_repages_only_after_v169():
    """R2-020 (optional caption): since V169 DQ_RECON_ERROR keys on the newest error-cycle day, so widening its
    WINDOW_HOURS look-back no longer re-pages old errors every morning. The claim is schema-gated (law 12)."""
    cap = _SRC[_SRC.index('st.caption("WINDOW_HOURS is informational'):]
    cap = cap[:cap.index("\n\n")]
    flat = re.sub(r'"\s*\n\s*"', "", cap)
    assert "reads (default 48h), and its alert text states it.\"" in flat
    assert ('+ (" Since V169 its event is keyed by the newest error-cycle day, so widening the look-back no '
            'longer re-pages old errors." if has_migration(169, _PAGE) else ""))') in flat
