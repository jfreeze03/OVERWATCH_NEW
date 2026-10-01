"""Test-wide safety: no test ever opens a real Snowflake session (PR A review r1 + r2).

app.core.session._connect falls back to st.connection("snowflake"), which reads the machine's default
connection (~/.snowflake/connections.toml or .streamlit/secrets.toml). Without this guard a plain
`pytest` run on a developer machine that HAS a default connection ran real reads and wrote real
APP_ERROR_LOG / APP_USAGE rows from the page harnesses and record_error. CI has no connection, so
refusing here makes every machine behave like CI.

SESSION-scoped (review r2): pytest sets up higher-scoped fixtures first, so a function-scoped guard let
module-scoped fixtures (tests/test_usage_sim.py's all-pages sweep) render against the real _connect.
Tests that monkeypatch get_session / get_cached_session / _connect themselves still override it for their
own body, and their monkeypatch restores the refuser afterwards. The script-mode `python tests/usage_sim.py`
LIVE variant is unaffected: conftest applies only under pytest.
"""

from __future__ import annotations

import os
import tempfile

import pytest

# Point the Snowflake connector's config home at an empty folder before anything imports it, and drop
# a default-connection override, so even a code path that bypasses _connect finds no connections.toml.
os.environ["SNOWFLAKE_HOME"] = tempfile.mkdtemp(prefix="overwatch-tests-no-snowflake-")
os.environ.pop("SNOWFLAKE_DEFAULT_CONNECTION_NAME", None)


def _wrap_single_select(value):
    # The streamlit 1.55 AppTest semantics: a single-select ButtonGroup holds one value (or None).
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _shim_apptest_buttongroup() -> None:
    """Back-port the streamlit 1.55 AppTest ButtonGroup fix onto the floor (streamlit==1.52.2, ci.yml).

    Below 1.55 the AppTest element tree reads a SINGLE-select st.segmented_control / st.pills value
    (every section switcher) as a list, so ``ButtonGroup.indices`` iterates the stored string per
    character and a re-run raises ``ValueError: content: "S" is not in list``. That is a test-harness
    bug, not an app bug -- and gating every shaped render on ``streamlit >= 1.55`` meant the floor leg,
    which exists to catch reliance on post-floor APIs before an SiS deploy, rendered no populated
    branch at all. This shim makes the floor harness serialize single-select values the way 1.55 does,
    so the shaped suites run on both legs. A no-op from 1.55 on; delete it once the floor reaches 1.55.
    """
    try:
        import streamlit
        from packaging.version import parse
    except ImportError:
        return
    if parse(streamlit.__version__) >= parse("1.55.0"):
        return
    from streamlit.proto.ButtonGroup_pb2 import ButtonGroup as _ButtonGroupProto
    from streamlit.testing.v1 import element_tree

    def _indices(self):
        values = self.value
        if self.proto.click_mode == _ButtonGroupProto.ClickMode.SINGLE_SELECT:
            values = _wrap_single_select(values)
        return [self.options.index(self.format_func(v)) for v in values]

    element_tree.ButtonGroup.indices = property(_indices)


_shim_apptest_buttongroup()


def _refuse_snowflake_session(*_args, **_kwargs):
    raise RuntimeError("tests never open a Snowflake session (tests/conftest.py)")


@pytest.fixture(scope="session", autouse=True)
def _no_real_snowflake_session():
    import app.core.session as session

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(session, "_connect", _refuse_snowflake_session)
        yield
