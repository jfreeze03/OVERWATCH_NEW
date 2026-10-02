"""The floor-compat leg renders the shaped AppTests (PR-2 R2-058 / R2-061).

Every shaped / section-switching AppTest used to carry ``skipif(streamlit < 1.55)`` for an AppTest
harness bug (a single-select ButtonGroup value read per character). The floor leg pins streamlit==1.52.2
-- the version SiS deploys -- so it rendered no populated branch at all, and a post-floor API inside one
(``st.metric(..., format=)``, ``st.iframe``) passed both legs and failed only in production. tests/conftest.py
now back-ports the 1.55 fix onto the floor harness and the gates are gone; these two locks keep it so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from packaging.version import parse  # noqa: E402

_TESTS = Path(__file__).resolve().parent


def test_conftest_shims_buttongroup_below_1_55_only():
    from streamlit.testing.v1 import element_tree

    indices = element_tree.ButtonGroup.indices.fget
    shimmed = indices.__qualname__.startswith("_shim_apptest_buttongroup.")
    assert shimmed == (parse(st.__version__) < parse("1.55.0"))
    if shimmed:   # the floor leg: a single-select value is one option, never iterated per character
        from types import SimpleNamespace

        from streamlit.proto.ButtonGroup_pb2 import ButtonGroup as _Proto
        single = SimpleNamespace(proto=SimpleNamespace(click_mode=_Proto.ClickMode.SINGLE_SELECT),
                                 options=["Spend", "Rules"], format_func=lambda v: v, value="Rules")
        assert indices(single) == [1]
        single.value = None
        assert indices(single) == []
        multi = SimpleNamespace(proto=SimpleNamespace(click_mode=_Proto.ClickMode.MULTI_SELECT),
                                options=["Spend", "Rules"], format_func=lambda v: v, value=["Rules", "Spend"])
        assert indices(multi) == [1, 0]


def test_no_test_regates_apptest_on_streamlit_1_55():
    flag = re.compile("_APPTEST_" + r"(?:BUTTONGROUP_)?OK\b")
    gate = re.compile(r"skipif\([^)]*1\." + r"55")
    offenders = [str(p.relative_to(_TESTS)) for p in sorted(_TESTS.rglob("*.py"))
                 if p.name != Path(__file__).name
                 and (flag.search(t := p.read_text(encoding="utf-8")) or gate.search(t))]
    assert not offenders, f"AppTest re-gated on streamlit 1.55 (the conftest shim covers the floor): {offenders}"
