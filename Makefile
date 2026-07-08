# OVERWATCH developer entry points. `make help` lists targets.
.PHONY: help install lint type test smoke check run

help:
	@grep -E '^[a-z]+:.*#' Makefile | sed 's/:.*#/ —/'

install:  # dev deps (runtime + toolchain)
	pip install -r requirements.txt -r requirements-dev.txt

lint:  # ruff, the style + blind-except gate
	ruff check .

type:  # mypy on the pure layers (logic/data/config)
	mypy

test:  # full suite, page smokes included
	pytest -q

smoke:  # fast: skip the AppTest page smokes
	pytest -q --ignore=tests/test_pages_apptest.py

check: lint type test  # everything CI runs

run:  # local app (needs .streamlit/secrets.toml — see secrets.toml.example)
	streamlit run streamlit_app.py

stress:  # render/logic stress harness (sandbox-relative timings)
	OW_STRESS=1 pytest tests/test_stress.py -q -s
