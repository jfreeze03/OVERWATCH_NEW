"""Test-wide safety: no test ever opens a real Snowflake session (PR A review r1).

app.core.session._connect falls back to st.connection("snowflake"), which reads the machine's default
connection (~/.snowflake/connections.toml or .streamlit/secrets.toml). Without this guard a plain
`pytest` run on a developer machine that HAS a default connection ran real reads and wrote real
APP_ERROR_LOG / APP_USAGE rows from the page harnesses and record_error. CI has no connection, so
refusing here makes every machine behave like CI. Tests that stub get_session / get_cached_session /
_connect themselves still override this (their monkeypatch runs after the autouse fixture).
The script-mode `python tests/usage_sim.py` LIVE variant is unaffected: conftest applies only under pytest.
"""

from __future__ import annotations

import os
import tempfile

import pytest

# Point the Snowflake connector's config home at an empty folder before anything imports it, and drop
# a default-connection override, so even a code path that bypasses _connect finds no connection.
os.environ["SNOWFLAKE_HOME"] = tempfile.mkdtemp(prefix="overwatch-tests-no-snowflake-")
os.environ.pop("SNOWFLAKE_DEFAULT_CONNECTION_NAME", None)


def _refuse_snowflake_session(*_args, **_kwargs):
    raise RuntimeError("tests never open a Snowflake session (tests/conftest.py)")


@pytest.fixture(autouse=True)
def _no_real_snowflake_session(monkeypatch):
    import app.core.session as session

    monkeypatch.setattr(session, "_connect", _refuse_snowflake_session)
