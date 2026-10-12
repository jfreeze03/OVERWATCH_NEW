"""The floor-compat leg installs its own pin list (.github/workflows/ci.yml), not requirements.txt.

2026-10-12: the leg pinned streamlit / pandas / altair / jinja2 but not pyarrow, which streamlit only bounds below
(pyarrow>=7.0). pyarrow 26.0.0 shipped needing NumPy 2, the floor's pandas 2.2.0 resolves NumPy 1.26.4, and main
went red (239 AppTest failures: "pyarrow requires NumPy 2.0 or newer"). A runtime library the floor leg leaves
unpinned drifts to whatever PyPI published last, which is not the floor. These locks keep pyarrow on
requirements.txt's own range in the floor install."""

from __future__ import annotations

import re

from tests._source import read


def _floor_install() -> str:
    ci = read(".github/workflows/ci.yml")
    job = ci[ci.index("  floor-compat:"):]
    job = job[:job.index("\n  # ", 1)] if "\n  # " in job[1:] else job
    m = re.search(r"pip install (.+?)\n\s*- name:", job, re.S)
    assert m, "the floor-compat install step was not found"
    return m.group(1)


def _requirement(name: str) -> str:
    for line in read("requirements.txt").splitlines():
        spec = line.split("#", 1)[0].strip()
        if re.match(rf"{re.escape(name)}\b", spec):
            return spec
    raise AssertionError(f"{name} is not in requirements.txt")


def test_the_floor_leg_pins_pyarrow_to_the_requirements_range():
    assert f'"{_requirement("pyarrow")}"' in _floor_install()


def test_the_floor_leg_keeps_its_runtime_floor_pins():
    install = _floor_install()
    for pin in ('"streamlit==1.52.2"', '"pandas==2.2.0"', '"altair==5.5.0"', '"jinja2==3.1.2"'):
        assert pin in install, pin
