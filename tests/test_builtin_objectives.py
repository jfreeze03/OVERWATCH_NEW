"""v4.597 (Decision Studio Option C, slice S2): Pipeline SLA built-in objectives.

Two read-only objectives on Operations > Pipeline SLA > Tonight replace the retired Decision
Studio SLO editor: "Nightly cycle done by <target> — n/N nights" (reuses the SLA finish forecast,
zero extra reads) and "Tasks on cadence — x/y" (one TASK_HISTORY batch member, byte-identical to
Tasks > SLA's so the two share the batch-member cache).
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pandas as pd

from app.logic.insights import (
    cycle_target_attainment,
    etl_cycle_sla_forecast,
    task_cadence_attainment,
    task_freshness_status,
)

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _fc(nights: list[dict], *, runway=None, target: str = "07:00") -> dict:
    """A minimal etl_cycle_sla_forecast result: ``nights`` newest-first, like the real one."""
    return {"nights": nights, "live_runway_sec": runway, "target_hhmm": target}


def _n(state: str, margin=None, spike=None) -> dict:
    return {"RUN_STATE": state, "MARGIN_SEC": margin, "EXPECTED_SPIKE": spike}


def test_met_late_failed_and_hung_nights_are_counted():
    fc = _fc([_n("COMPLETE", 600.0), _n("COMPLETE", -120.0), _n("FAILED"), _n("INCOMPLETE"),
              _n("COMPLETE", 0.0)])
    assert cycle_target_attainment(fc) == {"met": 2, "judged": 5, "late": 1, "failed": 1, "hung": 1,
                                           "target_hhmm": "07:00", "in_flight": False}


def test_tonights_run_is_excluded_only_while_it_still_has_runway():
    nights = [_n("INCOMPLETE"), _n("COMPLETE", 300.0), _n("COMPLETE", 60.0)]
    in_flight = cycle_target_attainment(_fc(nights, runway=3600.0))
    assert in_flight["judged"] == 2 and in_flight["met"] == 2 and in_flight["hung"] == 0
    assert in_flight["in_flight"] is True
    # past the target and still running -> a miss (hung), not an exclusion
    past = cycle_target_attainment(_fc(nights, runway=-60.0))
    assert past["judged"] == 3 and past["hung"] == 1 and past["in_flight"] is False
    # no snapshot (runway unknown) -> judged, never silently dropped
    unknown = cycle_target_attainment(_fc(nights, runway=None))
    assert unknown["judged"] == 3 and unknown["hung"] == 1
    # an OLDER incomplete night is always a miss (it never finished)
    older = cycle_target_attainment(_fc([_n("COMPLETE", 10.0), _n("INCOMPLETE")], runway=3600.0))
    assert older == {"met": 1, "judged": 2, "late": 0, "failed": 0, "hung": 1,
                     "target_hhmm": "07:00", "in_flight": False}


def test_spike_calendar_nights_are_judged_like_any_other():
    fc = _fc([_n("COMPLETE", -900.0, spike="month-end"), _n("COMPLETE", 900.0)])
    got = cycle_target_attainment(fc)
    assert got["judged"] == 2 and got["late"] == 1 and got["met"] == 1


def test_no_forecast_means_no_objective():
    assert cycle_target_attainment({}) == {}
    assert cycle_target_attainment(None) == {}
    assert cycle_target_attainment({"nights": []}) == {}
    assert cycle_target_attainment({"ok": True}) == {}


def _night(d: str, finish_hhmm: str, *, failed: int = 0, running: int = 0) -> dict:
    d0 = pd.Timestamp(d).normalize()
    fh, fm = (int(x) for x in finish_hhmm.split(":"))
    start = d0 + timedelta(hours=22)
    finish = d0 + timedelta(days=1, hours=fh, minutes=fm)
    return {"CYCLE_DATE": d0, "CYCLE_START": start, "CYCLE_FINISH": finish,
            "N_FAILED": failed, "N_RUNNING": running, "SNAPSHOT_TS": finish}


def test_attainment_reads_the_real_forecast_output():
    rows = [_night(f"2026-09-{d:02d}", "05:00") for d in range(1, 9)]
    rows[2] = _night("2026-09-03", "07:30")                  # late (after 07:00)
    rows[5] = _night("2026-09-06", "03:00", failed=2)        # failed
    fc = etl_cycle_sla_forecast(pd.DataFrame(rows), target_hhmm="07:00", breach_hhmm="08:00")
    got = cycle_target_attainment(fc)
    assert got["judged"] == len(fc["nights"]) == 8
    assert got["met"] == 6 and got["late"] == 1 and got["failed"] == 1 and got["hung"] == 0
    assert got["target_hhmm"] == "07:00"


def _fresh(statuses: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"TASK_NAME": [f"T{i}" for i in range(len(statuses))], "STATUS": statuses})


def test_task_cadence_attainment_counts_and_flags_the_cap():
    got = task_cadence_attainment(_fresh(["On-time", "On-time", "Late", "Stale", "On-time"]))
    assert got == {"on_time": 3, "total": 5, "late": 1, "stale": 1, "capped": False}
    assert task_cadence_attainment(_fresh(["On-time"] * 200))["capped"] is True     # LIMIT 200 hit
    assert task_cadence_attainment(_fresh(["On-time"] * 199))["capped"] is False
    assert task_cadence_attainment(_fresh(["Late"] * 10), row_cap=10)["capped"] is True
    assert task_cadence_attainment(None) == {}
    assert task_cadence_attainment(pd.DataFrame()) == {}


def test_task_cadence_reads_task_freshness_status_output():
    raw = pd.DataFrame({
        "DATABASE_NAME": ["D"] * 3, "SCHEMA_NAME": ["S"] * 3, "TASK_NAME": ["A", "B", "C"],
        "MEDIAN_GAP_MIN": [60.0, 60.0, 60.0], "LONG_GAP_MIN": [60.0, 60.0, 60.0],
        "MINS_SINCE_SUCCESS": [30.0, 110.0, None],       # on time / late (1x yard + lag) / silent
    })
    got = task_cadence_attainment(task_freshness_status(raw))
    assert got["total"] == 3 and got["on_time"] == 1 and got["late"] == 1 and got["stale"] == 1


def test_builder_cap_matches_the_attainment_default():
    from app.data import ops_sql
    assert ops_sql.task_freshness_sla().rstrip().endswith("LIMIT 200")


# ---------------------------------------------------------------------------------------------
# operations.py wiring locks
# ---------------------------------------------------------------------------------------------

def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def test_objectives_paint_first_but_reuse_the_forecast():
    ops = _src("app/ui/pages/operations.py")
    tonight = _body(ops, "_pipeline_tonight")
    slot = tonight.index("_obj_slot = st.container()")
    assert tonight.index("_tonight_glance_panel()") < slot
    fc_call = tonight.index("fc = _sla_finish_forecast_panel(pf=_pf)")
    assert slot < fc_call < tonight.index("with _obj_slot:")
    assert "_builtin_objectives_panel(fc, company, days, database, schema_contains)" in tonight
    # the forecast panel returns its fc ({} on every early exit) — the r8 locks still hold
    panel = _body(ops, "_sla_finish_forecast_panel")
    assert "def _sla_finish_forecast_panel(*, pf: dict | None = None) -> dict:" in ops
    # every early exit says WHY (review r1): setup (x3) / empty / unavailable-or-empty; never a bare {}.
    # PR-1 R1-059: the third setup exit is a zero-row all-time scan -- the starter workflow never ran,
    # a misnamed anchor -- which used to return "empty" under a green verified-clean row.
    assert panel.count('return {"_reason": "needs_setup"}') == 3 and 'return {"_reason": "empty"}' in panel
    assert 'return {"_reason": "unavailable" if not res.ok else "empty"}' in panel
    assert "return {}" not in panel and "return fc" in panel
    assert not re.search(r"\n\s+return\n", panel)                  # no bare return left
    # the tab threads company/schema into Tonight; the render() call string is unchanged
    assert "_pipeline_tonight(days, database, company, schema_contains)" in _body(ops, "_pipeline_sla_tab")
    assert ('_pipeline_sla_tab(is_operator, f["company"], f["database"], f["days"], '
            'f["schema_contains"])') in ops


def test_tasks_objective_shares_the_task_sla_batch_member():
    ops = _src("app/ui/pages/operations.py")
    member = "ops_sql.task_freshness_sla(max(days, 14), company, database, schema_contains)"
    assert ops.count(member) == 2
    for fn in ("_builtin_objectives_panel", "_task_sla_view"):
        body = _body(ops, fn)
        assert member in body and 'page=_PAGE, tier="recent")' in body
    panel = _body(ops, "_builtin_objectives_panel")
    assert '"source": "TASK_HISTORY (cadence + silence)"' in panel
    assert '_fres = _fb.get("fresh") if _fb is not None else None' in panel
    assert ") or {}" not in ops                                    # the r8 lock on this file
    assert ops.count("ACCOUNT_USAGE") == 42                         # budget unchanged


def test_objectives_panel_is_honest_about_absence_and_scope():
    ops = _src("app/ui/pages/operations.py")
    panel = _body(ops, "_builtin_objectives_panel")
    assert '_health = alarm_health(_misses) if (cyc or cad) else ""' in panel
    # review r1: a capped cadence read (LIMIT 200) is never shown green -- header or tile -- unless the cut
    # is PROVEN (v4.608: the Tasks ▸ SLA rule, TOTAL_TASKS > rows read and _freshness_cut_is_safe; render-
    # tested in tests/test_p608_ops_etl.py)
    assert 'if _health == "ok" and cad.get("unproven"):' in panel
    assert 'cad["unproven"] = bool(cad["capped"] and not _freshness_cut_is_safe(_fres.df))' in panel
    assert 'section_header("Built-in objectives", _health, "pipeline"' in panel
    assert '"" if cad.get("unproven") else "ok"' in panel
    assert '"value": "—"' in panel and '"needs setup"' in panel and "0/0" not in panel
    assert "execute_statement(" not in panel and "st.button(" not in panel   # read-only
    # join the source's adjacent string literals ("..." <newline> "..." and "..." + ("...")
    joined = re.sub(r'"\s*\n\s*(?:\+\s*\(?\s*)?"', "", panel)
    assert ("Read-only objectives derived from the ETL clock and each task's own cadence — no setup. "
            "The custom SLO editor was retired (v4.597); any ACTIVE SLO_OBJECTIVES rows still alert and "
            "badge the Entity 360 watchlist.") in joined
    # review r1: the cap is disclosed plainly (no "conservative read" claim -- a stopped task can sit outside)
    # PR-1 R1-129: the read ranks by silence RELATIVE to each task's cadence, so a stopped fast-cadence
    # task now leads it -- the caption names that order and drops the old "can fall outside" caveat
    assert "judged over the 200 tasks most overdue against their own cadence only" in joined
    assert "conservative" not in joined and "most-silent" not in joined
    contract = ops.split('"Pipeline SLA": {', 1)[1].split("},", 1)[0]
    assert "Dynamic-table refresh health honor Company/Database/Schema, as does " in contract
    # review r2: the builder clamps to 90 days, so the note and the tile help say so
    assert ("the Tasks-on-cadence objective, which reads its cadence over max(Window, 14) days, "
            "capped at 90.") in re.sub(r'"\s*\n\s*"', "", contract)
    assert 'f"over the last {min(max(days, 14), 90)} days."' in ops
    # the cap caption never promises a full list elsewhere: Tasks ▸ SLA reads the same LIMIT-200 query
    assert "for the full list" not in ops and "(Tasks ▸ SLA reads the same 200)" in joined
    # review r1: the Window moves the objective, so the contract lists it as partial (not "ignored")
    assert '"partial": ("company", "database", "schema_contains", "days")' in contract


def test_slo_breach_has_a_specific_playbook_naming_where_objectives_live():
    from app.logic.playbooks import PLAYBOOKS, playbook_for
    text = playbook_for("PERF_SLO_BREACH")
    assert text == PLAYBOOKS["PERF_SLO_BREACH"]
    assert "**Means:**" in text and "Built-in objectives" in text and "Pipeline SLA" in text
    assert "Entity 360" in text and "SLO_OBJECTIVES" in text
