"""Next-Fifty #14 Phase 1 wiring: the Tonight "Explain a task" drill on Operations ▸ Pipeline SLA.

Source locks (they run on the floor-compat leg; the shaped AppTest lives in test_pages_shaped.py): the
drill is toggle-gated and never prefetched, reads through its own on-demand run() on the 'recent' tier,
names no budgeted ACCOUNT_USAGE literal, renders at both Tonight sites, and the owner's
Informatica-side sentence has exactly one home. Plus the ETL-team ask doc stays consistent."""

from __future__ import annotations

import re

from tests._source import read

_OPS = "app/ui/pages/operations.py"


def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def test_evidence_drill_is_toggle_gated() -> None:
    body = _body(read(_OPS), "_task_evidence_drill")
    toggle = body.index("st.toggle(")
    assert toggle < body.index("run(") and toggle < body.index("st.selectbox(")
    assert 'key=f"{key}_toggle", value=False' in body
    # the off state renders nothing else: the toggle's `if not ...: return` is the first statement after it
    gate = body[toggle:].split("):\n", 1)[1].lstrip()
    assert gate.startswith("return\n"), gate[:80]
    assert "st.caption(" not in body[:toggle] and "empty_state(" not in body[:toggle]


def test_evidence_drill_source_label_avoids_the_budgeted_literal() -> None:
    body = _body(read(_OPS), "_task_evidence_drill")
    assert "ACCOUNT_USAGE" not in body
    assert 'source="CONTROL_STATUS x QUERY_HISTORY (task evidence, on demand)"' in body
    for banned in ("st.info(", "st.success(", "st.dataframe(", ") or {}", "methodology_note(",
                   "ACCOUNT_USAGE_LAG_NOTE"):
        assert banned not in body, banned


def test_evidence_drill_links_the_call_profile_and_reads_recent() -> None:
    body = _body(read(_OPS), "_task_evidence_drill")
    assert 'snowsight_profile_column(disp, _PAGE, id_col="CALL_QUERY_ID")' in body
    assert 'tier="recent"' in body and "max_rows=etl_control_sql.MAX_EVIDENCE_ROWS" in body
    assert "etl_control_sql.run_task_evidence_scan(fqn, task=task, workflow=workflow, run_id=run_id," in body
    assert "floor_days=floor" in body and "today=account_today()" in body
    # every finding and absence is escaped at its markdown sink (error text can carry '$')
    assert 'st.error(md_dollars("🔴 " + line.text))' in body
    assert 'st.warning(md_dollars("🟠 " + line.text))' in body
    assert "md_dollars(line.text)" in body and "md_dollars(line.hint)" in body


def test_both_tonight_sites_render_the_drill() -> None:
    src = read(_OPS)
    runtimes = _body(src, "_workflow_runtimes_panel")
    # review F22: the runtimes site binds the run its table shows (it used to pass no run_id, so the drill
    # re-derived 'the latest run' at its own read time — a newer run than a cached table's at cycle start)
    flat = " ".join(runtimes.split())
    assert ('_task_evidence_drill(fqn, df, workflow=_wf_pick, run_id=evidence_run_id(df), days=days, '
            'key="etl_ev_rt")') in flat
    assert runtimes.index("result_caption(res)") < runtimes.index("_task_evidence_drill(")
    # RUN_ID is the binding, not a column shown in the table (the panel renders df otherwise as-is)
    assert 'styled_table(df.drop(columns=["RUN_ID"], errors="ignore"), height=320)' in runtimes
    assert "styled_table(df, height=320)" not in runtimes
    inventory = _body(src, "_run_inventory_panel")
    assert '_task_evidence_drill(status_fqn, tres.df, run_id=picked, key="etl_ev_inv")' in inventory
    assert inventory.index("result_caption(tres)") < inventory.index("_task_evidence_drill(")
    assert src.count("_task_evidence_drill(") == 3          # the def + the two sites


def test_evidence_picker_remembers_the_pick_outside_the_widget() -> None:
    """Review F12/F20: on SiS's streamlit 1.52 a selectbox's identity includes its option labels, so a
    re-labelled option (a new attempt or failure after a 5-minute refresh) re-creates the widget at
    `index`. The pick is remembered in its own key and seeds `index` (not part of the keyed identity)."""
    body = _body(read(_OPS), "_task_evidence_drill")
    prev = body.index('_prev = st.session_state.get(f"{key}_last")')
    idx = body.index("_idx = opts.index(_prev) if _prev in opts else 0")
    pick = body.index('task = st.selectbox("Task to explain", opts, index=_idx, key=f"{key}_pick",')
    save = body.index('st.session_state[f"{key}_last"] = task')
    assert prev < idx < pick < save < body.index("run(")
    assert "index=0" not in body[pick:save]
    # the labels carry no live runtime (a running task's RUNTIME_SEC changes every refresh)
    labels = read("app/logic/etl_evidence.py").split("def evidence_task_labels(", 1)[1].split("\ndef ", 1)[0]
    assert "humanize_duration" not in labels and "rec[2]" not in labels


def _picker_render(monkeypatch):
    """Render ONLY the drill (toggle on) through AppTest. No ButtonGroup widget on this path, so it also runs
    on the streamlit 1.52 floor leg — the version where a re-labelled selectbox resets."""
    import pandas as pd
    import pytest
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.core.result import QueryResult
    from app.ui.pages import operations

    seen: list[str] = []

    def _run(*args, **kwargs):
        seen.append(str(args[0] if args else kwargs.get("sql", "")))
        return QueryResult(df=pd.DataFrame(), ok=True, source="stub")

    monkeypatch.setattr(operations, "run", _run)

    def _app():
        import pandas as _pd
        import streamlit as _st

        from app.ui.pages import operations as _ops
        extra = int(_st.session_state.get("_evt_extra_attempts", 0))
        rows = [{"TASK_NAME": "SP_A", "TASK_STATUS": "SUCCEEDED", "RUNTIME_SEC": 900.0},
                {"TASK_NAME": "SP_B", "TASK_STATUS": "SUCCEEDED", "RUNTIME_SEC": 300.0}]
        rows += [{"TASK_NAME": "SP_A", "TASK_STATUS": "SUCCEEDED", "RUNTIME_SEC": 60.0}] * extra
        _ops._task_evidence_drill("DB.SCH.CONTROL_STATUS", _pd.DataFrame(rows), run_id="R1", key="evt")

    at = AppTest.from_function(_app, default_timeout=30)
    at.session_state["evt_toggle"] = True
    at.run()
    assert not at.exception, at.exception
    return at, seen


def test_evidence_picker_survives_a_relabel(monkeypatch) -> None:
    at, seen = _picker_render(monkeypatch)
    assert at.selectbox(key="evt_pick").value == "SP_A"                   # slowest first
    at.selectbox(key="evt_pick").set_value("SP_B").run()
    assert at.selectbox(key="evt_pick").value == "SP_B"
    # the next refresh shows a retry of SP_A: its label becomes 'SP_A · 2 attempts'
    seen.clear()
    at.session_state["_evt_extra_attempts"] = 1
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox(key="evt_pick").value == "SP_B", "the pick snapped back when a label changed"
    assert seen and "TASK_NAME = 'SP_B'" in seen[-1]                    # the drill still reads the picked task
    # picking again still works after the re-label
    at.selectbox(key="evt_pick").set_value("SP_A").run()
    assert at.selectbox(key="evt_pick").value == "SP_A"


def test_evidence_is_never_prefetched() -> None:
    src = read(_OPS)
    assert "run_task_evidence_scan" not in _body(src, "_pipeline_prefetch")
    wants = re.findall(r"_pipeline_prefetch\(days, want=\{([^}]*)\}\)", src)
    assert wants and not any("evidence" in w for w in wants)
    assert "task-evidence drill" in _body(src, "_pipeline_prefetch")        # the EXCLUDED note says why


def test_pointer_captions_route_to_the_drill() -> None:
    src = read(_OPS)
    assert "Tonight ▸ Run inventory ▸ pick the run ▸ Explain a task" in _body(src, "_failure_recurrence_panel")
    assert "Tonight ▸ Workflow runtimes ▸ Explain a task" in _body(src, "_workflow_drift_panel")


def test_informatica_side_sentence_has_one_home() -> None:
    sentence = "No failed Snowflake CALL found in the window — the failure was likely on the Informatica side"
    assert sentence in " ".join(read("app/logic/etl_evidence.py").split())
    assert sentence not in read(_OPS)


def test_query_tag_ask_doc_is_consistent() -> None:
    doc = read("docs/design/INFORMATICA_QUERY_TAG_ASK.md")
    for needle in ("ALTER SESSION SET QUERY_TAG", "UNSET QUERY_TAG", '"pipeline"', '"run_id"', '"task"',
                   "ETL_COST_TAGS.md", "CONTROL_STATUS.RUN_ID", "GET_PATH(TRY_PARSE_JSON(QUERY_TAG)"):
        assert needle in doc, needle
    assert "INFORMATICA_QUERY_TAG_ASK.md" in read("docs/design/ETL_COST_TAGS.md")
    # the verdict's hint and the builder docstring both point the ETL team at the same doc
    assert "docs/design/INFORMATICA_QUERY_TAG_ASK.md" in read("app/logic/etl_evidence.py")
    assert "docs/design/INFORMATICA_QUERY_TAG_ASK.md" in read("app/data/etl_control_sql.py")


def test_tag_keys_unchanged_by_the_ask() -> None:
    from app.data import etl_sql
    # Phase 1 is docs-only for the new optional 'task' key; the readers' key tuple is untouched
    assert "task" not in etl_sql.TAG_KEYS
