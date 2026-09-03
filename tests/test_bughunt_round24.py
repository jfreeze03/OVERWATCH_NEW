"""Regression locks for bug-hunt round 24 — false-all-clear, chart precision, non-idempotent write, deep-link scope.

REPLAY    control_room day-replay rendered a green "quiet day" verdict even when a read FAILED (the
          `activity`/query-health read has no sub-panel, so its failure vanished into the all-clear).
          Now demotes to a partial warning naming the domains that could not be read.
HEATMAP   hour_heatmap's "Hottest" takeaway rounded a genuinely-fractional AVG_CREDITS to an integer
          ("(0)" for the brightest cell). Given a value_fmt param (mirroring bar_count v4.457); the
          two AVG_CREDITS callers pass ",.3f".
SLA       Pipeline-SLA registration was a bare INSERT into a keyed config table with no edit UI, so
          re-registering (the only way to change an SLA) double-wrote the row. Now MERGEs on the key
          + reruns.
WH-JUMP   the Overview spend-driver bar click and the Jump-to WH pick routed to Operations ▸ Warehouses,
          which ignores warehouse_contains; now route to Queries (which honors it), like the DB pick.
INV-NOTE  the alert Investigate→ arrival note said "Filters applied" even when the destination ignores
          a carried filter (contradicting its own "Active but ignored" banner); reworded "Scope set".
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- REPLAY: a failed read is not a clean all-clear ----------------------------------------------
def test_day_replay_demotes_the_clean_verdict_on_a_failed_read():
    src = _src("app/ui/pages/control_room.py")
    assert '_failed = [n for n, r in' in src
    assert "elif _failed:" in src
    assert "this verdict is partial" in src
    # the green clean all-clear is now the else branch (only when nothing failed)
    assert 'empty_state("clean", f"{day_iso}: a quiet day' in src


# --- HEATMAP: fractional AVG_CREDITS survives the takeaway ----------------------------------------
def test_hour_heatmap_takeaway_uses_a_value_format():
    charts = _src("app/ui/charts.py")
    assert 'value_fmt: str = ",.0f"' in charts
    assert "format(float(_v.iloc[_p]), value_fmt)" in charts          # takeaway
    assert 'alt.Tooltip("Value:Q", format=value_fmt)' in charts       # tooltip
    # the hard-coded ,.0f that rounded 0.214 -> "0" is gone from the takeaway
    assert "float(_v.iloc[_p]):,.0f" not in charts
    for rel in ("app/ui/pages/operations.py", "app/ui/pages/cost_parts/optimize.py"):
        assert 'value_fmt=",.3f"' in _src(rel)


# --- SLA: idempotent upsert instead of a duplicating INSERT ---------------------------------------
def test_pipeline_sla_registration_merges_and_reruns():
    src = _src("app/ui/pages/operations.py")
    assert "MERGE INTO" in src and "PIPELINE_SLA_CONFIG" in src
    assert "WHEN MATCHED THEN UPDATE SET MAX_AGE_HOURS" in src
    # the bare INSERT that double-wrote on re-registration is gone
    assert "INSERT INTO {core_object('PIPELINE_SLA_CONFIG')} " not in src
    # non-idempotent write must rerun so the updated list is the receipt (notify docstring contract)
    seg = src.split("MERGE INTO", 1)[1].split("if not is_operator:", 1)[0]
    assert "st.rerun()" in seg


# --- WH-JUMP: warehouse jumps land where warehouse_contains is honored ----------------------------
def test_warehouse_jumps_route_to_queries_not_warehouses():
    ov = _src("app/ui/pages/overview.py")
    assert 'request_navigation("Operations", "Queries",\n                                       {"warehouse_contains": _picked_wh})' in ov
    main = _src("app/main.py")
    assert 'request_navigation("Operations", "Queries", {"warehouse_contains": name})' in main
    # neither jump routes to the warehouse-contains-ignoring Warehouses tab any more
    assert 'request_navigation("Operations", "Warehouses", {"warehouse_contains"' not in main


# --- INV-NOTE: the arrival note does not over-claim ------------------------------------------------
def test_investigate_arrival_note_says_scope_set_not_filters_applied():
    src = _src("app/ui/pages/alerts.py")
    assert '"filter_note": (f"Scope set from alert ' in src
    assert '"filter_note": (f"Filters applied from alert ' not in src
