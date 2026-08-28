"""UI/UX master list — Wave 2 Snowsight-link batch (C35 + F27).

Locks: C35 the table layer auto-attaches the Snowsight query-profile link to any
frame carrying real QUERY_IDs (manual call sites no-op — PROFILE already there) ·
F27 warehouses / databases / table objects get an outbound Snowsight URL, wired
into Entity 360 as the native-console complement to the in-app drill.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.ui import components
from app.ui.components import snowsight_object_url

_ROOT = Path(__file__).resolve().parents[1]
_COMP = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
_WB = (_ROOT / "app" / "ui" / "workbench.py").read_text(encoding="utf-8")


def _seed_ctx(monkeypatch):
    fake = SimpleNamespace(session_state={"_ow_snowsight_ctx": ("myorg", "myacct")})
    monkeypatch.setattr(components, "st", fake)


# ---- F27: object URLs --------------------------------------------------------

def test_object_urls_for_the_three_stable_kinds(monkeypatch):
    _seed_ctx(monkeypatch)
    base = "https://app.snowflake.com/myorg/myacct/#"
    assert snowsight_object_url("WAREHOUSE", "wh_alfa_etl", "P") == \
        base + "/compute/warehouses/WH_ALFA_ETL"
    assert snowsight_object_url("DATABASE", "alfa_db", "P") == \
        base + "/data/databases/ALFA_DB"
    assert snowsight_object_url("OBJECT", "db.sch.my_table", "P") == \
        base + "/data/databases/DB/schemas/SCH/table/MY_TABLE"


def test_object_urls_degrade_honestly(monkeypatch):
    _seed_ctx(monkeypatch)
    assert snowsight_object_url("OBJECT", "db.only_two", "P") == ""   # not a 3-part FQN
    assert snowsight_object_url("TASK", "db.sch.t", "P") == ""        # no stable page
    assert snowsight_object_url("WAREHOUSE", "", "P") == ""           # blank key
    # unresolved session context -> no link, never a dead one
    monkeypatch.setattr(components, "_snowsight_ctx", lambda page: None)
    assert snowsight_object_url("WAREHOUSE", "WH", "P") == ""


def test_entity_360_offers_the_native_console_jump():
    assert "snowsight_object_url(kind, key, _PAGE)" in _WB
    assert "Open in Snowsight" in _WB


# ---- C35: auto profile links -------------------------------------------------

def test_table_layer_auto_attaches_profile_links():
    idx = _COMP.index("gets the Snowsight profile link\n    # AUTOMATICALLY")
    block = _COMP[idx:idx + 800]
    assert '"QUERY_ID" in getattr(df, "columns", ())' in block
    assert '"PROFILE" not in df.columns' in block             # manual sites no-op
    assert 'st.session_state.get("_ow_dl_page")' in block     # page identity gate
    assert "snowsight_profile_column(df, _prof_page)" in block


def test_ctx_probe_is_shared_and_failure_is_never_cached():
    # one probe serves profile links AND object links; a failed probe must not
    # pin ('','') for the session (R3-3).
    assert "def _snowsight_ctx(" in _COMP
    probe = _COMP.split("def _snowsight_ctx(", 1)[1].split("\ndef ", 1)[0]
    assert 'st.session_state["_ow_snowsight_ctx"] = ctx' in probe
    assert probe.index("return None") < probe.index('st.session_state["_ow_snowsight_ctx"] = ctx')


# ---- review fixes: encoding, link_button, probe backoff ----------------------

def test_hostile_keys_are_percent_encoded(monkeypatch):
    # quoted Snowflake identifiers can carry spaces/parens/# — every path
    # segment is percent-encoded so a key can never break out of the URL.
    _seed_ctx(monkeypatch)
    url = snowsight_object_url("WAREHOUSE", 'MYWH) [X](https://evil.example', "P")
    assert ")" not in url.split("#", 1)[1] and "]" not in url and " " not in url
    assert "%29" in url                                     # the ')' is encoded
    spaced = snowsight_object_url("DATABASE", "My DB", "P")
    assert " " not in spaced and "%20" in spaced


def test_entity_link_renders_via_link_button_not_markdown():
    # link_button keeps the URL out of markdown parsing entirely.
    assert 'st.link_button("Open in Snowsight' in _WB
    assert "[Open in Snowsight" not in _WB                  # no markdown link form


def test_failed_probe_backs_off_instead_of_flooding():
    # with the probe auto-wired into every QUERY_ID table, a failure must not
    # re-fire (and error-log) per table per rerun — 5-minute backoff, probe=True.
    probe = _COMP.split("def _snowsight_ctx(", 1)[1].split("\ndef ", 1)[0]
    assert '"_ow_snowsight_ctx_failed_at"' in probe
    assert "< 300" in probe
    assert "probe=True" in probe
    # a success clears the failure marker so links come back immediately
    assert 'st.session_state.pop("_ow_snowsight_ctx_failed_at", None)' in probe
