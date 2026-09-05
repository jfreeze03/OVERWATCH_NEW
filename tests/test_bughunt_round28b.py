"""Bug-hunt round 28b: twin-divergence completion (the dimension round 28's finder
missed on a transient API error). 5 confirmed CODE-FIXABLE divergences fixed; [5] refuted
(already disclosed); [1]/[2] left as owner-gated mart-loader decisions (see CHANGELOG).

Each defect below is a same-metric mart-path vs live-path divergence where the number the
user saw flipped with mart warmth, or a served-path caveat was never surfaced:

#4 (MED) Control Room ▸ triage queue dropped the Schema filter on the WARM mart path
   (fact_task_daily called without f["schema_contains"]) while the live fallback applied it.
#3 (MED) Operations ▸ Tasks ▸ Health mislabeled a live-capped (<=90d) count as the full
   requested window — the sibling Queries tile already corrected this via a served-window.
#6 (MED) Security ▸ Trust Center fallback KPI counted clean critical/high-severity scanners
   (TOTAL_AT_RISK_COUNT==0); the mart/delta path gates on active>0. Same tile, two counts.
#7 (LOW) Operations ▸ Contention pressure panel never surfaced its source, hiding that
   P95_ELAPSED_SEC is peak-hourly on the mart but window-p95 on the live fallback.
#8 (LOW) failed_logins_fact counted the loader's COALESCE(CLIENT_IP,'(none)') sentinel as a
   distinct source IP, inflating the spray signal by 1 vs the live twin (which drops NULL).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _region(src: str, start: str, end: str) -> str:
    a = src.index(start)
    b = src.index(end, a + len(start))
    return src[a:b]


# --- #4: the warm mart path carries the schema filter, like the live fallback --------

def test_control_room_triage_mart_task_read_passes_schema_filter():
    src = _read("app/ui/pages/control_room.py")
    reg = _region(src, "section_header(\"Triage queue\")", "wh_daily = run(")
    # the mart read now passes f["schema_contains"] (4 positional args), matching the
    # live fallback one line below and the Operations task-health twin
    assert 'fact_task_daily(2, company, f["database"], f["schema_contains"])' in reg
    # the live fallback still filters by schema too (unchanged) — both paths agree
    assert 'task_runs(2, company, f["database"], f["schema_contains"]' in reg


# --- #3: the Tasks tile labels the SERVED window, not the raw request ----------------

def test_operations_task_health_labels_served_window_on_live_cap():
    src = _read("app/ui/pages/operations.py")
    body = _region(src, "def _task_health_view", "def ")
    # honesty mirrors the Queries tile (:_served_days): live path clamps to the live window
    assert "_tr_served = days if _from_mart else min(days, MAX_LIVE_WINDOW_DAYS)" in body
    assert 'f"{_tr_served}d"' in body
    # the raw "{days}d" label is gone from the tile
    assert 'else f"{days}d"' not in body


# --- #6: Trust Center fallback counts only AT-RISK scanners, like the mart path -------

def test_trust_center_fallback_gates_kpis_on_at_risk_count():
    src = _read("app/ui/pages/security.py")
    body = _region(src, "trust_center_findings()", "_GOV_SIGNALS")
    assert 'at_risk = pd.to_numeric(fdf["TOTAL_AT_RISK_COUNT"], errors="coerce").fillna(0).gt(0)' in body
    assert '_crit = (sev == "CRITICAL") & at_risk' in body
    assert '_high = (sev == "HIGH") & at_risk' in body
    # the un-gated counts (which double-counted clean critical-severity scanners) are gone
    assert "(sev == 'CRITICAL').sum()" not in body
    assert "(sev == 'HIGH').sum()" not in body
    # the fallback frame really does carry the column the gate reads
    assert "TOTAL_AT_RISK_COUNT" in _region(
        _read("app/data/security_sql.py"), "def trust_center_findings", "\ndef ")


# --- #7: the contention pressure panel surfaces its (peak-hourly vs window) p95 source -

def test_contention_pressure_panel_discloses_source():
    src = _read("app/ui/pages/operations.py")
    reg = _region(src, 'section_header("Warehouse queue & spill pressure"', 'section_header("Lock waits"')
    assert "result_caption(res)" in reg
    # the mart_source it surfaces carries the peak-hourly caveat
    assert "p95 is peak hourly" in reg


# --- #8: the fact spray signal drops the '(none)' sentinel to match the live twin -----

def test_failed_logins_fact_excludes_none_sentinel_from_distinct_ips():
    body = _region(_read("app/data/security_sql.py"),
                   "def failed_logins_fact", "\ndef failed_login_reasons_fact")
    assert "COUNT(DISTINCT NULLIF(CLIENT_IP, '(none)')) AS DISTINCT_IPS" in body
    # the live twin (raw LOGIN_HISTORY, real NULLs) keeps the plain form — no sentinel there
    live = _region(_read("app/data/security_sql.py"), "def failed_logins(", "\ndef ")
    assert "COUNT(DISTINCT CLIENT_IP) AS DISTINCT_IPS" in live
