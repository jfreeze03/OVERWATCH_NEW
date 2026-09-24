"""Shared morning-attention reads (Next-Fifty #1, wave 1) — ONE read path for the Brief and the
Control Room verdicts, and the Operations ▸ Pipeline ▸ Tonight glance. Cache identity is (tier,
capped SQL, scope); page/key are telemetry-only, so every surface lands on the SAME run() cache
entries. All reads are config-gated and FAIL-SILENT (probe=True); the Operations panels own the
setup/grant hints. Reads CONTROL_STATUS + the XLAT/staging tables only.

Do NOT move these reads into a run_batch prefetch: batch members cache in a separate store that
run() never consults, so the cross-page hit would be lost."""

from __future__ import annotations

from app.core.query import run
from app.core.result import QueryResult
from app.data import etl_control_sql
from app.logic.insights import cycle_night_summary, etl_cycle_sla_forecast


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
    frame; the verdicts fold it via cycle_night_summary."""
    fqn = str(settings.get("ETL_CONTROL_STATUS_FQN") or "").strip()
    if not fqn:
        return None
    scan_sql = etl_control_sql.cycle_night_health_scan(
        fqn, start_workflow=str(settings.get("ETL_CYCLE_START_WORKFLOW") or "").strip())
    if not scan_sql:
        return None
    return run(scan_sql, page=page, key="attn_cycle_night", tier="recent",
               source="CONTROL_STATUS (tonight, every workflow)",
               max_rows=etl_control_sql.MAX_NIGHT_WORKFLOWS, probe=True)


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
    """{ref_gap_n, ref_gap_label, night, cycle} — the ETL half of the shared attention bundle."""
    ref_n, ref_label = reference_gap_summary(settings, page=page)
    res = cycle_night_read(settings, page=page)
    night = cycle_night_summary(res.df) if (res is not None and res.ok and not res.empty) else {}
    return {"ref_gap_n": ref_n, "ref_gap_label": ref_label, "night": night,
            "cycle": nightly_cycle_forecast(settings, page=page)}
