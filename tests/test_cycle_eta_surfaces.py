"""Next-Fifty #36 wiring locks (source): tonight's projected finish and the cycle timeline reuse the reads the
morning surfaces already make. Runs on both CI legs (the shaped AppTests in tests/test_prc_c2_shaped.py skip on
the streamlit 1.52.2 floor), so every wiring claim is locked here by source too."""

from __future__ import annotations

import re

from app.logic.playbooks import PLAYBOOKS
from tests._source import read

_ATTN = read("app/ui/attention.py")
_BRIEF = read("app/ui/pages/brief.py")
_OPS = read("app/ui/pages/operations.py")


def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def _joined(src: str) -> str:
    """Adjacent string literals joined (a caption split across source lines reads as one sentence)."""
    return re.sub(r'"\s*\n\s*f?"', "", src)


def test_attention_passes_the_terminal_and_folds_the_eta_without_a_read():
    night = _body(_ATTN, "cycle_night_read")
    assert 'end_workflow=str(settings.get("ETL_CYCLE_END_WORKFLOW") or "").strip())' in night
    etl = _body(_ATTN, "etl_attention")
    assert '"eta": etl_cycle_eta(cycle, night_df,' in etl
    assert "run(" not in etl                                   # the ETA folds reads, never makes one
    assert len(re.findall(r"\brun\(scan_sql", _ATTN)) == 3      # ref gap, night, cycle finish: unchanged
    assert "def cycle_night_read(settings: dict, *, page: str)" in _ATTN


def test_the_night_read_falls_back_to_the_pre_eta_roll_up():
    night = _body(_ATTN, "cycle_night_read")
    # the enriched read first, then (only for a fault the base SQL can answer) the pre-#36 roll-up
    first = night.index('key="attn_cycle_night"')
    assert first < night.index("eta_columns=False") < night.index('key="attn_cycle_night_base"')
    assert night.count("probe=True") == 2
    assert "if res.ok or res.error_kind not in _NIGHT_FALLBACK_KINDS:" in night
    assert "absent" not in _ATTN.split("_NIGHT_FALLBACK_KINDS = ", 1)[1].split("\n", 1)[0]
    assert "record_error(" in night and "_night_fallback_logged" in night


def test_brief_passes_the_shared_eta():
    assert 'eta=_etl.get("eta")' in _BRIEF
    kpi = _body(_BRIEF, "_nightly_cycle_kpi")
    assert "eta: dict | None = None" in kpi and "$" not in kpi
    # 'Overdue' stays first inside the in-flight branch
    assert kpi.index('_tile("Overdue"') < kpi.index('if eta and eta.get("ok"):')


def test_tonight_order_reuses_the_glance_read():
    tonight = _body(_OPS, "_pipeline_tonight")
    chain = ["_night_res = _tonight_glance_panel()", "_eta_slot = st.container()", "_obj_slot = st.container()",
             "fc = _sla_finish_forecast_panel(pf=_pf)", "with _eta_slot:", "_cycle_eta_panel(fc, _night_res)",
             "with _obj_slot:", "_cycle_timeline_panel(_night_res)", "_workflow_runtimes_panel(days, pf=_pf)"]
    idx = [tonight.index(s) for s in chain]
    assert idx == sorted(idx), chain
    glance = _body(_OPS, "_tonight_glance_panel")
    assert "def _tonight_glance_panel() -> QueryResult | None:" in _OPS
    assert "return None" in glance and glance.count("return res") == 2
    assert not re.search(r"\n\s+return\n", glance)             # every exit hands the read on


def test_eta_and_timeline_panels_make_no_reads():
    for name in ("_cycle_eta_panel", "_cycle_timeline_panel"):
        body = _body(_OPS, name)
        assert not re.search(r"\brun\(", body), name
        for banned in ("run_batch(", "etl_control_sql.cycle_", "st.info(", "st.success(", "methodology_note(",
                       "st.selectbox("):
            assert banned not in body, (name, banned)
    eta = _body(_OPS, "_cycle_eta_panel")
    assert "etl_cycle_eta(fc, night_res.df if (night_res is not None and night_res.usable()) else None," in eta
    assert '{"breach": "bad", "miss": "warn", "running_long": "warn"}.get(' in eta
    assert "humanize_duration(abs(_vs), 's')" in eta and "humanize_duration(abs(_lf), 's')" in eta
    # an unknown pace (the fallback frame) never reads as "nothing finished yet"
    assert 'if eta.get("pace_available")' in eta and "pace unavailable from tonight's roll-up" in eta
    tl = _body(_OPS, "_cycle_timeline_panel")
    assert '"END_OFFSET_SEC" not in night_res.df.columns' in tl       # hidden on the fallback frame
    assert tl.index('section_header("Cycle timeline"') < tl.index("st.toggle(")
    assert 'styled_table(tl, height=360, slug="etl_cycle_timeline")' in tl
    assert "etl_control_sql.MAX_NIGHT_WORKFLOWS" in tl                  # the cap caption


def test_pace_tile_reads_the_labelled_night_adjusted_lateness():
    """PR C review C1: the Pace tile shows the lateness that moves 'at this pace' (pace_late_adj_sec: less the
    marker's expected share of a labelled night's typical extra) and names the allowance."""
    eta = _body(_OPS, "_cycle_eta_panel")
    assert '_adj = eta.get("pace_late_adj_sec")' in eta
    assert "_lf = safe_float(_late if _adj is None else _adj)" in eta
    assert 'eta.get("pace_spike_share_sec")' in eta and "night " in eta and "allowed)" in eta


def test_timeline_toggle_is_static_and_floor_safe():
    tl = _body(_OPS, "_cycle_timeline_panel")
    assert ('st.toggle("Show tonight\'s cycle timeline", key="ops_cycle_timeline_toggle", value=False,'
            in tl)


def test_disclosure_names_the_v156_differences():
    text = _joined(_body(_OPS, "_cycle_eta_panel"))
    for want in ("How this differs from the PIPE_ETL_CYCLE_LATE alert", "14 prior clean nights",
                 "month-end nights included", "first clean finish", "hard deadline",
                 "leaves out month- and quarter-end nights", "the two times can differ"):
        assert want in text, want


def test_operations_budgets_hold():
    assert _OPS.count("ACCOUNT_USAGE") == 42
    assert ") or {}" not in _OPS
    assert _OPS.count("methodology_note(") == 3
    assert "The SLA finish forecast, projected finish and cycle timeline use fixed 14-night" in _OPS


def test_playbook_names_the_new_panels():
    late = PLAYBOOKS["PIPE_ETL_CYCLE_LATE"]
    assert "*Tonight's projected finish*" in late and "*Cycle timeline*" in late
    assert "can differ" in late and "$" not in late
