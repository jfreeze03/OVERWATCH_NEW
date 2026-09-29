"""Shared morning-attention reads (Next-Fifty #1, wave 1) — ONE read path for the Brief and the
Control Room verdicts, and the Operations ▸ Pipeline ▸ Tonight glance. Cache identity is (tier,
capped SQL, scope); page/key are telemetry-only, so every surface lands on the SAME run() cache
entries. All reads are config-gated and FAIL-SILENT (probe=True); the Operations panels own the
setup/grant hints. Reads CONTROL_STATUS + the XLAT/staging tables only.

Do NOT move these reads into a run_batch prefetch: batch members cache in a separate store that
run() never consults, so the cross-page hit would be lost."""

from __future__ import annotations

from app.core.query import record_error, run
from app.core.result import QueryResult
from app.data import etl_control_sql
from app.logic.insights import cycle_night_summary, etl_cycle_eta, etl_cycle_sla_forecast

# Next-Fifty #36: failure kinds of the ETA-enriched night read that the pre-#36 roll-up can still
# answer (a compile / identifier / function fault in the additive columns). 'absent' (no table or no
# grant) and 'timeout' fail the base read the same way, so they never pay a second round trip.
_NIGHT_FALLBACK_KINDS = frozenset({"missing_column", "unknown_function", "other"})
_night_fallback_logged: set[str] = set()


def reference_gap_summary(settings: dict, *, page: str) -> tuple[int, str]:
    """Source codes with no XLAT translation across every configured check.

    Reuses the Operations ▸ Pipeline reference-gap scan (etl_control_sql), run
    account-wide — pinned checks plus every configured check, no Database filter,
    the widest morning read. Config-gated and FAIL-SILENT: unset config, no valid
    check, or a missing SELECT grant (a probe read → the 'absent' branch, so it is
    neither error-logged nor counted as a failed fetch) all return (0, "") so the
    Brief never shows a setup or grant hint — the Operations panel owns that.
    Returns (code_count, label) where label names the affected check type(s),
    e.g. 'pc_uwissuetype.code'."""
    xlat = str(settings.get("ETL_REF_GAP_XLAT") or "").strip()
    raw = str(settings.get("ETL_REF_GAP_CHECKS") or "").strip()
    if not xlat or not raw:
        return 0, ""
    checks, _ = etl_control_sql.parse_ref_gap_checks(raw)
    scan_sql, _ = etl_control_sql.reference_gap_scan(checks, xlat)
    if not scan_sql:
        return 0, ""
    res = run(scan_sql, page=page, key="attn_ref_gaps", tier="recent",
              source="staging tables MINUS XLAT reference",
              max_rows=etl_control_sql.MAX_CODES, probe=True)
    if not (res.ok and not res.empty) or "CHECK_NAME" not in res.df.columns:
        return 0, ""
    types = list(dict.fromkeys(res.df["CHECK_NAME"].astype(str)))
    label = ", ".join(types) if len(types) <= 2 else f"{types[0]}, {types[1]} +{len(types) - 2} more"
    return len(res.df), label


def cycle_night_read(settings: dict, *, page: str) -> QueryResult | None:
    """The whole-night roll-up read (None when unconfigured / invalid FQN). Operations renders the
    frame; the verdicts fold it via cycle_night_summary.

    Next-Fifty #36: the terminal workflow rides along (the pace marker's upper bound), so the SQL is
    identical for the Brief, the Control Room and Operations and the run() cache stays shared. Safety
    net: this is the hottest shared read and a probe (a missing-column fault is silent), so when the
    ETA-enriched SQL fails for a reason the pre-#36 roll-up can answer, the roll-up is re-read without
    the ETA columns: the failed / did-not-run signals never blank, and the projected finish and the
    cycle timeline simply stay hidden. The fault is logged once per process so it is not silent."""
    fqn = str(settings.get("ETL_CONTROL_STATUS_FQN") or "").strip()
    if not fqn:
        return None
    start_wf = str(settings.get("ETL_CYCLE_START_WORKFLOW") or "").strip()
    scan_sql = etl_control_sql.cycle_night_health_scan(
        fqn, start_workflow=start_wf,
        end_workflow=str(settings.get("ETL_CYCLE_END_WORKFLOW") or "").strip())
    if not scan_sql:
        return None
    res = run(scan_sql, page=page, key="attn_cycle_night", tier="recent",
              source="CONTROL_STATUS (tonight, every workflow)",
              max_rows=etl_control_sql.MAX_NIGHT_WORKFLOWS, probe=True)
    if res.ok or res.error_kind not in _NIGHT_FALLBACK_KINDS:
        return res
    base_sql = etl_control_sql.cycle_night_health_scan(fqn, start_workflow=start_wf, eta_columns=False)
    base = run(base_sql, page=page, key="attn_cycle_night_base", tier="recent",
               source="CONTROL_STATUS (tonight, every workflow; projected finish unavailable)",
               max_rows=etl_control_sql.MAX_NIGHT_WORKFLOWS, probe=True)
    if not base.ok:
        return res
    _sig = f"{res.error_kind}:{str(res.error)[:120]}"
    if _sig not in _night_fallback_logged:
        _night_fallback_logged.add(_sig)
        record_error(page, RuntimeError(f"cycle_night ETA columns failed; served the base roll-up: {res.error}"),
                     context="attention.cycle_night_read fallback")
    return base


def nightly_cycle_forecast(settings: dict, *, page: str) -> dict:
    """Whole-cycle SLA finish forecast for the Brief 'Nightly cycle' tile (fail-silent probe read).

    Reuses the Operations SLA-finish builder (cycle_finish_history_scan) + etl_cycle_sla_forecast: the
    cycle STARTER workflow → the TERMINAL workflow, each night's finish vs the clock deadline, trended.
    The anchor workflows + clock times default in DEFAULT_SETTINGS, so this works before V138 is applied.
    Config-gated on ETL_CONTROL_STATUS_FQN + both anchor workflows; returns {} when unconfigured or when
    there is no cycle data (a probe read — a missing grant is silent; Operations ▸ Pipeline owns hints)."""
    fqn = str(settings.get("ETL_CONTROL_STATUS_FQN") or "").strip()
    start_wf = str(settings.get("ETL_CYCLE_START_WORKFLOW") or "").strip()
    end_wf = str(settings.get("ETL_CYCLE_END_WORKFLOW") or "").strip()
    if not fqn or not start_wf or not end_wf:
        return {}
    scan_sql = etl_control_sql.cycle_finish_history_scan(
        fqn, start_workflow=start_wf, end_workflow=end_wf)
    if not scan_sql:
        return {}
    res = run(scan_sql, page=page, key="attn_cycle_finish", tier="recent",
              source="CONTROL_STATUS (cycle finish forecast)",
              max_rows=etl_control_sql.MAX_SLA_NIGHTS, probe=True)
    if not (res.ok and not res.empty):
        return {}
    fc = etl_cycle_sla_forecast(
        res.df,
        target_hhmm=str(settings.get("ETL_SLA_TARGET_HHMM") or "07:00").strip(),
        breach_hhmm=str(settings.get("ETL_SLA_BREACH_HHMM") or "08:00").strip(),
        spike_calendar=str(settings.get("EXPECTED_SPIKE_CALENDAR") or ""))
    return fc or {}


def etl_attention(settings: dict, *, page: str) -> dict:
    """{ref_gap_n, ref_gap_label, night, cycle, eta} — the ETL half of the shared attention bundle.

    ``eta`` (Next-Fifty #36) is tonight's projected finish folded from the two reads above — no read
    of its own ({} when the cycle is not in flight)."""
    ref_n, ref_label = reference_gap_summary(settings, page=page)
    res = cycle_night_read(settings, page=page)
    night_df = res.df if (res is not None and res.ok and not res.empty) else None
    night = cycle_night_summary(night_df) if night_df is not None else {}
    cycle = nightly_cycle_forecast(settings, page=page)
    return {"ref_gap_n": ref_n, "ref_gap_label": ref_label, "night": night, "cycle": cycle,
            "eta": etl_cycle_eta(cycle, night_df,
                                 end_workflow=str(settings.get("ETL_CYCLE_END_WORKFLOW") or "").strip())}
