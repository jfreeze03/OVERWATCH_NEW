# AGENTS.md

## Cursor Cloud specific instructions

OVERWATCH is a single Python 3.11 Streamlit app (`streamlit_app.py` → `app.main:main`)
that reads from a managed Snowflake account. There is no database, docker-compose,
or sidecar service to run locally — just the Streamlit process. See `README.md`,
`ARCHITECTURE.md`, and `DEPLOYMENT.md` for product/architecture detail, and the
`Makefile` for the canonical dev commands (`make lint|type|test|run`).

### Environment
- Dependencies live in a virtualenv at `/workspace/.venv` (the base image only ships
  Python 3.12, which is externally-managed/PEP-668, so a venv is required). The startup
  update script creates it and installs `requirements.txt` + `requirements-dev.txt`.
- Run tools through the venv, e.g. `.venv/bin/pytest -q`, `.venv/bin/ruff check .`,
  `.venv/bin/mypy`, `.venv/bin/streamlit run streamlit_app.py`. The `~/.bashrc` also
  auto-activates this venv for new interactive shells.
- CI (`.github/workflows/ci.yml`) and `mypy.ini` target Python **3.11**; local dev here
  runs the same pinned deps on Python **3.12**. Lint/type/tests all pass on 3.12.

### Running / testing
- Tests, lint, and mypy are fully offline — the `app/logic`/`app/data` layers are
  Streamlit- and Snowflake-free by design, and the page-smoke suite
  (`tests/test_pages_apptest.py`) stubs the query layer. No Snowflake needed to test.
- `pytest.ini` sets `pythonpath = .`, so run the bare `pytest` entrypoint (not
  `python -m pytest`) from the repo root; the first-party `app` package is never pip-installed.
- The Streamlit dev server listens on port **8501** (`.streamlit/config.toml` sets
  `server.headless = true`).

### Snowflake connection (expected local limitation)
- Real data requires a live Snowflake account configured via a `[connections.snowflake]`
  section in `.streamlit/secrets.toml` (see `.streamlit/secrets.toml.example`). Snowflake
  is a managed cloud service and cannot be self-hosted.
- Without credentials the app **still boots** but renders an honest "No Snowflake
  connection." screen. This is expected behavior, not a crash. By design there is no
  synthetic/mock data, so data-backed page flows cannot be exercised in the browser
  without valid Snowflake credentials.
