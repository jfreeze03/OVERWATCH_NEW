"""#33: Admin stops calling 300 s "the default" statement timeout.

300 s is what V002 SETS on the app warehouse (CREATE WAREHOUSE ... STATEMENT_TIMEOUT_IN_SECONDS = 300
plus an explicit ALTER). Snowflake's own default, when neither the warehouse nor the account sets
it, is 172800 s (48h). An empty SHOW PARAMETERS level means exactly that case, so it reads
"Snowflake default", not "ACCOUNT (default)". The V002 value is derived from the migration, never
hand-pinned.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_RETIRED_WORDING = (
    "300s default", "300s account default", "300s by default", "(or the account; 300s",
    "that 300s wall", "300s warehouse default", '"ACCOUNT (default)"',
)


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Join wrapped lines (and continued `#` comments) so a phrase split across lines still matches."""
    return re.sub(r"[ \t]*\n[ \t]*#?[ \t]*", " ", text)


def test_the_300s_default_wording_is_gone():
    for rel in ("app/ui/pages/admin.py", "app/core/session.py"):
        flat = _flat(_src(rel))
        for phrase in _RETIRED_WORDING:
            assert phrase not in flat, f"{rel}: still says {phrase!r}"


def test_flattening_catches_a_wrapped_phrase():
    # negative control: the pre-#33 session.py comment wrapped "300s warehouse" / "# default"
    assert "300s warehouse default" in _flat("the real wall is the 300s warehouse\n# default), so")
    assert "300s by default" in _flat("STATEMENT_TIMEOUT_IN_SECONDS — 300s by\n    default — which")


def test_timeout_constants_are_derived_and_humanized():
    from app.logic.formulas import humanize_duration
    from app.ui.pages import admin
    v002 = _src("snowflake/migrations/V002__facts.sql")
    m = re.search(r"ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = (\d+);", v002)
    assert m, "V002 no longer sets the app warehouse timeout explicitly"
    assert int(m.group(1)) == admin._V002_APP_WH_TIMEOUT_S      # derived from V002, not hand-pinned
    assert admin._SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S == 172_800
    assert humanize_duration(admin._SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S, "s") == "48h"


def test_performance_tab_names_the_real_levels():
    adm = _src("app/ui/pages/admin.py")
    perf = adm.split("def _performance_tab(", 1)[1].split("\ndef ", 1)[0]
    assert '"Snowflake default"' in perf
    assert "humanize_duration(_V002_APP_WH_TIMEOUT_S, 's')" in perf
    assert "humanize_duration(_SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S, 's')" in perf
    # both the panel help and the unreadable-parameter fallback name V002 and Snowflake's default
    assert perf.count("_V002_APP_WH_TIMEOUT_S") >= 2 and perf.count("_SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S") >= 2
