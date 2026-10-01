"""R1-206: Action Center / Entity 360 render a failed read by its KIND.

needs_setup ("V074 is pending / required", "not installed", "needs V010") is ONLY for a true absence
(app.core.result.is_setup_absence). A timeout, schema drift or any other failure on an installed
ACTION_QUEUE / ENTITY_CATALOG / change registry is empty_state("unavailable", ..., detail=<error>) --
never a setup claim, never an absence claim ("no ownership record yet", "no action is linked"), and the
lifecycle / ownership editors stay off a failed read (a blank ownership form over a failed read would
MERGE blanks over the existing record).

Rendered with the tests/test_probe_absence_split.py fakes (_failed / _ok).
"""

from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import pytest

from tests.test_probe_absence_split import _FAILED, _SETUP, _failed, _FakeSt, _ok


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _PageSt(_FakeSt):
    """_FakeSt plus the widgets the two pages touch (nothing clicked, nothing typed)."""

    def __init__(self, entity_key: str = ""):
        super().__init__()
        self.session_state = {"entity_360_key": entity_key}

    def columns(self, spec, *_a, **_k):
        return [_Ctx() for _ in range(spec if isinstance(spec, int) else len(spec))]

    def selectbox(self, _label, options, *_a, key: str = "", **_k):
        opts = list(options)
        return "WAREHOUSE" if key == "entity_360_type" else (opts[0] if opts else "")

    def text_input(self, *_a, key: str = "", **_k):
        return self.session_state.get(key, "")

    def button(self, *_a, **_k):
        return False

    def link_button(self, *_a, **_k):
        return None

    def code(self, *_a, **_k):
        return None

    @contextmanager
    def status(self, *_a, **_k):
        yield self


def _patch_page(monkeypatch, results: dict, *, entity_key: str = ""):
    from app.ui import workbench as wb
    fake = _PageSt(entity_key)
    seen: dict = {"runs": [], "empty": [], "detail": [], "editor": [], "kpis": [], "tables": []}

    def fake_run(_sql, *_a, key: str = "", **_k):
        seen["runs"].append(key)
        return results.get(key, _ok(pd.DataFrame()))

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    monkeypatch.setattr(wb, "st", fake)
    monkeypatch.setattr(wb, "run", fake_run)
    monkeypatch.setattr(wb, "empty_state", fake_empty)
    for name, value in {
        "section_header": lambda *_a, **_k: None, "read_model_caption": lambda *_a, **_k: None,
        "kpi_row": lambda items, *_a, **_k: seen["kpis"].append(items),
        "styled_table": lambda df, *_a, **_k: seen["tables"].append(df),
        "status_chips": lambda *_a, **_k: None, "exception_summary": lambda *_a, **_k: None,
        "master_detail": lambda *_a, **_k: None, "viewer_name": lambda: "",
        "navigation_context": lambda: {}, "snowsight_object_url": lambda *_a, **_k: "",
        "filters": lambda: {"days": 30}, "load_settings": lambda *_a, **_k: {},
        "evidence_gate": lambda *_a, **_k: False, "is_operator": lambda: True,
        "_render_catalog_editor": lambda *a, **_k: seen["editor"].append(a),
    }.items():
        monkeypatch.setattr(wb, name, value)
    return wb, fake, seen


# ------------------------------------------------------------------------------ Action Center ----

@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_action_center_failed_read_splits_on_the_kind_and_never_claims_v074_pending(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {"action_center_ALL_True": _failed(kind)})
    wb.render_action_center("ALL")
    # one read: the dead pre-V074 legacy fallback (and its false "V074 is pending" banner) is gone
    assert seen["runs"] == ["action_center_ALL_True"]
    ((state, msg),) = seen["empty"]
    assert "V074 is pending" not in msg
    if kind in _SETUP:
        assert state == "needs_setup" and "not installed or not readable" in msg
    else:
        assert state == "unavailable" and "could not be read" in msg
        assert seen["detail"] == [f"boom ({kind})"]
    assert not seen["kpis"]                          # no counts over a failed read


def test_action_center_source_has_no_read_failure_fallback():
    from tests._source import read
    body = read("app/ui/workbench.py").split("def render_action_center", 1)[1].split("\ndef ", 1)[0]
    assert "action_center_legacy_" not in body and "V074 is pending. Showing" not in body
    assert "is_setup_absence(extended_res.error_kind)" in body


# ------------------------------------------------------------------------------ Entity 360 -------

@pytest.mark.parametrize("kind", _FAILED)
def test_entity_360_failed_record_read_is_unavailable_and_hides_the_editor(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {"entity_record_WAREHOUSE_WH_A": _failed(kind)},
                                  entity_key="WH_A")
    wb.render_entity_360("ALL")
    states = dict(seen["empty"])
    assert "no_data_yet" not in states or states["no_data_yet"] != "This entity has no ownership record yet."
    assert ("needs_setup", "V074 is required for the ownership catalog and watchlists.") not in seen["empty"]
    (msg,) = [m for s, m in seen["empty"] if s == "unavailable"]
    assert "ownership record could not be read" in msg
    assert f"boom ({kind})" in seen["detail"]
    # the full-replace ownership MERGE is never offered over a failed read (it would write blanks)
    assert seen["editor"] == []


@pytest.mark.parametrize("kind", _SETUP)
def test_entity_360_absent_catalog_is_setup(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {"entity_record_WAREHOUSE_WH_A": _failed(kind)},
                                  entity_key="WH_A")
    wb.render_entity_360("ALL")
    assert ("needs_setup", "V074 is required for the ownership catalog and watchlists.") in seen["empty"]
    assert not [m for s, m in seen["empty"] if s == "unavailable"]
    assert seen["editor"] == []


def test_entity_360_ok_empty_record_keeps_the_no_record_state_and_the_editor(monkeypatch):
    wb, _fake, seen = _patch_page(monkeypatch, {}, entity_key="WH_A")
    wb.render_entity_360("ALL")
    assert ("no_data_yet", "This entity has no ownership record yet.") in seen["empty"]
    assert len(seen["editor"]) == 1


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_entity_360_changes_and_linked_work_split(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {"entity_changes_WAREHOUSE_WH_A": _failed(kind),
                                                "entity_actions_WAREHOUSE_WH_A": _failed(kind)},
                                  entity_key="WH_A")
    wb.render_entity_360("ALL")
    msgs = [m for _s, m in seen["empty"]]
    assert "No action is linked to this entity." not in msgs     # a failed read is not an absence
    assert ("unavailable", "Linked work could not be read for this entity.") in seen["empty"]
    if kind in _SETUP:
        assert ("needs_setup", "Change tracking needs the change-impact scan (V010).") in seen["empty"]
    else:
        assert ("unavailable", "Recent changes could not be read for this entity.") in seen["empty"]
        assert "Change tracking needs the change-impact scan (V010)." not in msgs


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_data_product_detail_split(monkeypatch, kind):
    wb, _fake, seen = _patch_page(monkeypatch, {"product_detail_CLAIMS": _failed(kind)})
    wb._render_data_product_detail("CLAIMS")
    ((state, msg),) = seen["empty"]
    if kind in _SETUP:
        assert (state, msg) == ("needs_setup", "V074 is required for the ownership catalog.")
    else:
        assert state == "unavailable" and "could not be read" in msg
        assert seen["detail"] == [f"boom ({kind})"]
