"""Regression locks for bug-hunt round 22 — empty-vs-zero sweep, silent-failure, sort-label, cross-filter.

STORAGE   storage_account_truth is a bare aggregate (DAYS_AVERAGED=0 on no data), so keying the
          mart->live fallback + the no-data note on res.empty rendered a fabricated "$0.00/mo".
          Both now gate on DAYS_AVERAGED.
ALLIN     org_all_in_window_usd is a bare aggregate; an unlanded org-usage window rendered
          "$0.00" as a measured invoice. The tile now gates on TOTAL_USD being non-NULL.
INCIDENT  Bulk incident-resolve reported the stale render-time open_now as "Resolved N" even on a
          0-row UPDATE (execute_statement can't see the rowcount). Reports honestly now.
PROC-TREND The v4.455 leaderboard warehouse/user fix did not reach the per-proc trend drill, breaking
          its "always agree" invariant. proc_cost_trend now honors warehouse/user too.
PROC-SLA  The "Busiest procedures" table is primary-sorted failing-first (deliberate) but labeled
          "calls × p95 desc"; relabeled to disclose the failing-first tier.
BREAKGLASS The Break-glass panel is account-wide by design but the Changes section declares
          Database/Schema applied; marked "(account-wide)" like its siblings.
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- STORAGE: the fallback + no-data note gate on DAYS_AVERAGED, not the (never-empty) frame -----
def test_account_storage_gates_on_days_averaged_not_empty():
    src = _src("app/ui/pages/cost_parts/spend.py")
    gate = 'safe_float(res.df.iloc[0].get("DAYS_AVERAGED"), default=0.0) <= 0'
    # both the mart->live fallback trigger and the no-data note use the DAYS_AVERAGED gate
    assert src.count(gate) >= 2
    assert "if not res.ok or res.empty or " + gate + ":" in src


# --- ALLIN: the all-in tile is omitted when the org read has no landed rows (TOTAL_USD NULL) ------
def test_all_in_tile_gates_on_total_usd_non_null():
    src = _src("app/ui/pages/cost_parts/spend.py")
    assert ('if allin_res.usable() and not allin_res.df.empty '
            'and pd.notna(allin_res.df.iloc[0].get("TOTAL_USD")):') in src


# --- INCIDENT: bulk resolve does not assert a stale count ----------------------------------------
def test_bulk_incident_resolve_reports_honestly():
    src = _src("app/ui/pages/control_room.py")
    assert 'f"Resolved {open_now:,} open incident(s) in scope."' not in src
    assert "Resolved the open (OPEN/MITIGATED) incidents in this" in src


# --- PROC-TREND: the trend drill honors warehouse/user like the leaderboard (behavioral) ---------
def test_proc_cost_trend_honors_warehouse_and_user():
    filtered = insights_sql.proc_cost_trend(
        "SP_X", 30, "ALFA", warehouse_contains="ETLWH", user_contains="svcacct")
    assert "ETLWH" in filtered and "svcacct" in filtered
    assert "USER_NAME" in filtered              # USER_NAME appears ONLY via the new filter
    plain = insights_sql.proc_cost_trend("SP_X", 30, "ALFA")
    assert "ETLWH" not in plain and "svcacct" not in plain
    assert "USER_NAME" not in plain
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    # the panel threads the page's warehouse/user filters into the trend call — 3 sites now carry
    # it (both procedure_costs_usd calls from v4.455 + the new proc_cost_trend drill).
    assert "proc_cost_trend(" in uc
    assert uc.count('user_contains=f["user_contains"]') >= 3


# --- PROC-SLA: the sort label discloses the failing-first primary sort ----------------------------
def test_proc_sla_rollup_sort_label_discloses_failing_first():
    src = _src("app/ui/pages/operations.py")
    assert 'sort_label="failing first, then calls × p95"' in src
    assert 'sort_label="calls × p95 desc"' not in src


# --- BREAKGLASS: the account-wide panel carries the marker its siblings use -----------------------
def test_breakglass_panel_marked_account_wide():
    src = _src("app/ui/pages/security.py")
    assert "Break-glass role activity (account-wide; should hug zero)" in src
