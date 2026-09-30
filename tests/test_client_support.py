"""Security > Clients: Snowflake's driver support floor (Next-Fifty #34) and the four shipped tab defects.

Pure logic (app/logic/client_support.py): version keys, support status, who upgrades, the tolerant
SYSTEM$CLIENT_VERSION_INFO() parser and the KPI counts/wording. An EXECUTED sqlite harness runs the real
client_drivers SQL over sessions seeded from the owner's 2026-09-29 probe (answers W4/W4c), proving:
SQLAPI 2.0.0 is not BEHIND and bare 'SQLAPI' is NO VERSION (never newest); Snowflake's own web app and
services read SNOWFLAKE-RUN and never set a customer's newest version; NULL/blank client ids are
'(no client id)', never green CURRENT. The harness models Snowflake's NULL ordering (DEFAULT_NULL_ORDERING
= LAST: NULLs first on DESC), which sqlite reverses, so dropping an explicit NULLS LAST fails the executed
tests too, not only the string lock in history_locks/test_live_round4. Render tests drive _clients_tab
with fakes: a working floor read gives 3 yours-to-upgrade / 2 Snowflake-run UNSUPPORTED; a failed one, one
whose entries list no minimum (key drift) and a capped inventory read with dashed KPIs, never clean.

The JSON fixture is modelled on Snowflake's documented SYSTEM$CLIENT_VERSION_INFO() shape with the floor
values the probe returned (W4c). The owner has not pasted the raw W3 JSON yet; swap it in when they do.
Snowflake's version numbers appear here as FIXTURES only; the app never hard-codes them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.data import canary, security_sql
from app.logic import client_support as cs
from app.ui import status_colors

sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]

# The documented shape (an array of per-client objects) with the floors W4c returned on this account.
_W3_JSON = json.dumps([
    {"clientId": "DOTNETDriver", "clientAppId": ".NET", "minimumSupportedVersion": "2.0.9",
     "minimumNearingEndOfSupportVersion": "2.0.11", "recommendedVersion": "4.1.0", "deprecatedVersions": [],
     "_customSupportedVersions_": []},
    {"clientId": "GO", "clientAppId": "Go", "minimumSupportedVersion": "1.11.2",
     "minimumNearingEndOfSupportVersion": "1.12.1", "recommendedVersion": "2.2.0", "deprecatedVersions": []},
    {"clientId": "JDBC", "clientAppId": "JDBC", "minimumSupportedVersion": "3.19.1",
     "minimumNearingEndOfSupportVersion": "3.20.1", "recommendedVersion": "4.3.4", "deprecatedVersions": []},
    {"clientId": "JSDriver", "clientAppId": "JavaScript", "minimumSupportedVersion": "1.14.0",
     "minimumNearingEndOfSupportVersion": "1.15.0", "recommendedVersion": "3.3.0", "deprecatedVersions": []},
    {"clientId": "ODBC", "clientAppId": "ODBC", "minimumSupportedVersion": "3.5.0",
     "minimumNearingEndOfSupportVersion": "3.5.0", "recommendedVersion": "3.21.0", "deprecatedVersions": []},
    {"clientId": "PythonConnector", "clientAppId": "PythonConnector", "minimumSupportedVersion": "3.12.3",
     "minimumNearingEndOfSupportVersion": "3.12.4", "recommendedVersion": "4.7.5", "deprecatedVersions": []},
    {"clientId": "SQLAPI", "clientAppId": "SQLAPI", "minimumSupportedVersion": "2.0.0",
     "minimumNearingEndOfSupportVersion": "2.0.0", "recommendedVersion": "2.0.0", "deprecatedVersions": []},
])


def _floors() -> list[cs.ClientFloor]:
    return cs.parse_client_version_info(_W3_JSON)


# ---------------------------------------------------------------------------
# version_key / support_status / who_upgrades / client_label
# ---------------------------------------------------------------------------

def test_version_key_pads_segments_and_rejects_no_version():
    assert cs.version_key("3.13.22") == (3, 13, 22, 0)
    assert cs.version_key("2") == cs.version_key("2.0.0") == (2, 0, 0, 0)
    assert cs.version_key("3..1") == (3, 0, 1, 0)                     # an empty segment reads 0, as in SQL
    assert cs.version_key("3.0.0-beta") == (3, 0, 0, 0)
    assert cs.version_key("1.2.3.4.5") == (1, 2, 3, 4, 5)
    for blank in ("?", "", "   ", None, float("nan"), pd.NA, "SQLAPI"):
        assert cs.version_key(blank) is None, blank
    assert cs.version_key("3.10.2") > cs.version_key("3.9.1")          # numeric, not text order


@pytest.mark.parametrize(("version", "floor", "expected"), [
    ("3.13.22", ("3.19.1", "3.20.1", "4.3.4"), cs.UNSUPPORTED),        # JDBC, AWS Glue
    ("1.1.5", ("1.11.2", "1.12.1", "2.2.0"), cs.UNSUPPORTED),          # Go, Snowflake web app
    ("3.10.1", ("3.12.3", "3.12.4", "4.7.5"), cs.UNSUPPORTED),         # Python connector, Trexis Glue
    ("3.2.2", ("3.5.0", "3.5.0", "3.21.0"), cs.UNSUPPORTED),           # ODBC, Power BI gateway
    ("3.25.0", ("3.19.1", "3.20.1", "4.3.4"), cs.BELOW_RECOMMENDED),   # Informatica: supported
    ("3.12.3", ("3.12.3", "3.12.4", "4.7.5"), cs.NEARING_EOS),         # at the minimum, below nearing-EOS
    ("2.0.0", ("2.0.0", "2.0.0", "2.0.0"), cs.OK),                     # SQLAPI 2.0.0 (ControlM) is OK
    ("2.2.0", ("1.11.2", "1.12.1", "2.2.0"), cs.OK),
    ("?", ("2.0.0", "2.0.0", "2.0.0"), cs.NO_VERSION),                 # bare 'SQLAPI': never compared
    ("?", (None, None, None), cs.NOT_LISTED),                          # Snowsight: no entry
    ("4.0.0", ("", "", ""), cs.NOT_LISTED),                            # an entry missing every key
    ("1.0.0", (None, None, "2.0.0"), cs.BELOW_RECOMMENDED),            # only the keys it has
])
def test_support_status_on_the_probe_rows(version, floor, expected):
    assert cs.support_status(version, *floor) == expected


def test_who_upgrades_is_a_small_allow_list_that_defaults_to_yours():
    assert cs.who_upgrades("Go", "Snowflake Web App (CNG)") == cs.WHO_SNOWFLAKE
    assert cs.who_upgrades("Go", "snowflake web app") == cs.WHO_SNOWFLAKE
    assert cs.who_upgrades("Snowsight", "Snowflake Web App (snowsight_x)") == cs.WHO_SNOWFLAKE
    assert cs.who_upgrades("SnowServices Ingress", "(not reported)") == cs.WHO_SNOWFLAKE
    assert cs.who_upgrades(" snowsight ", None) == cs.WHO_SNOWFLAKE
    for driver, program in (("ODBC", "MashupEngineGateway"), ("Go", "Go"), ("Go", "[ADBC]MashupEngine"),
                            ("JavaScript", "cortex_code_cli"), ("(no client id)", "(not reported)"),
                            ("", ""), (None, None)):
        assert cs.who_upgrades(driver, program) == cs.WHO_YOURS, (driver, program)
    # the SQL twin uses ILIKE '<prefix>%': a wildcard inside a prefix would widen it silently
    assert not any(ch in p for p in cs.SNOWFLAKE_RUN_PROGRAM_PREFIXES for ch in "%_")


def test_client_labels_by_program_prefix():
    cases = {"INFA_DI_Cloud": "Informatica", "INFA_IICS": "Informatica", "MashupEngineGateway": "Power BI gateway",
             "[ADBC]MashupEngine": "Power BI (ADBC)", "com.amazonaws.services.glue.P": "AWS Glue",
             "terraform-provider-snowflake": "Terraform", "posit_workbench_rstudio": "Posit",
             "cortex_code_cli": "Cortex Code CLI", "cortex_code_sa": "Cortex Code CLI",
             "PythonConnector": "", "(not reported)": "", "": ""}
    for program, label in cases.items():
        assert cs.client_label(program) == label, program
    assert cs.driver_display("PythonConnector") == "Python connector" and cs.driver_display("JDBC") == "JDBC"


# ---------------------------------------------------------------------------
# the SYSTEM$CLIENT_VERSION_INFO() parser
# ---------------------------------------------------------------------------

def test_parser_reads_the_documented_json():
    floors = {f.client_app_id: f for f in _floors()}
    assert set(floors) == {".NET", "Go", "JDBC", "JavaScript", "ODBC", "PythonConnector", "SQLAPI"}
    jdbc = floors["JDBC"]
    assert (jdbc.min_supported, jdbc.nearing_eos, jdbc.recommended) == ("3.19.1", "3.20.1", "4.3.4")
    assert floors["JavaScript"].client_id == "JSDriver"


def test_parser_tolerates_key_spellings_missing_keys_and_junk():
    raw = [{"clientID": "X1", "MinimumSupportedVersion": "1.0"},                 # only clientId, odd case
           {"client_app_id": "Y", "min_supported_version": "2.0", "recommended_version": "3.0"},
           {"clientAppId": "Z"},                                                   # no version keys at all
           {"minimumSupportedVersion": "9.9"},                                      # names no client: dropped
           "not an object", 7]
    floors = cs.parse_client_version_info(raw)
    assert [(f.client_id, f.client_app_id) for f in floors] == [("X1", ""), ("", "Y"), ("", "Z")]
    assert floors[1].min_supported == "2.0" and floors[1].recommended == "3.0"
    index = cs.floor_index(floors)
    assert set(index) == {"X1", "Y", "Z"}
    z = index["Z"]
    assert cs.support_status("1.0", z.min_supported, z.nearing_eos, z.recommended) == cs.NOT_LISTED
    assert cs.parse_client_version_info("not json") == [] and cs.parse_client_version_info(None) == []
    assert len(cs.parse_client_version_info({"clients": json.loads(_W3_JSON)})) == 7   # wrapped array


def test_client_app_id_beats_client_id_in_the_index():
    floors = cs.parse_client_version_info([{"clientId": "Go", "clientAppId": "GoOther", "minimumSupportedVersion": "9"},
                                           {"clientId": "GO_ID", "clientAppId": "Go", "minimumSupportedVersion": "1"}])
    assert cs.floor_index(floors)["GO"].min_supported == "1"


def test_frame_parser_fills_a_missed_field_from_raw_entry():
    df = pd.DataFrame([
        {"CLIENT_ID": "JDBC", "CLIENT_APP_ID": "JDBC", "MIN_SUPPORTED_VERSION": "3.19.1",
         "NEARING_EOS_VERSION": "3.20.1", "RECOMMENDED_VERSION": "4.3.4",
         "RAW_ENTRY": json.dumps({"clientAppId": "IGNORED", "minimumSupportedVersion": "0.0.1"})},
        # Snowflake renamed a key: the flatten read NULL, the entry's own JSON still has it
        {"CLIENT_ID": None, "CLIENT_APP_ID": None, "MIN_SUPPORTED_VERSION": None, "NEARING_EOS_VERSION": None,
         "RECOMMENDED_VERSION": None,
         "RAW_ENTRY": json.dumps({"client_app_id": "ODBC", "minSupportedVersion": "3.5.0"})},
        {"CLIENT_ID": None, "CLIENT_APP_ID": None, "MIN_SUPPORTED_VERSION": None, "NEARING_EOS_VERSION": None,
         "RECOMMENDED_VERSION": None, "RAW_ENTRY": "garbage"},
    ])
    floors = cs.floors_from_frame(df)
    assert [(f.client_app_id, f.min_supported) for f in floors] == [("JDBC", "3.19.1"), ("ODBC", "3.5.0")]


def test_read_floors_never_turns_a_failed_or_empty_read_into_a_verdict():
    for kind in ("absent", "unknown_function", "missing_column", "timeout", "other"):
        floors, reason = cs.read_floors(False, pd.DataFrame(), f"{kind} boom\nmore detail", kind)
        assert floors is None and reason == f"{kind} boom", kind
    assert cs.read_floors(False, None, "", "unknown_function") == (
        None, "the function is not available to the app's owner role")
    assert cs.read_floors(True, pd.DataFrame(), "", "")[0] is None             # zero entries: not clean
    assert cs.read_floors(True, pd.DataFrame([{"RAW_ENTRY": "x"}]), "", "")[0] is None
    ok, why = cs.read_floors(True, pd.DataFrame([{"CLIENT_APP_ID": "JDBC", "MIN_SUPPORTED_VERSION": "1"}]))
    assert ok and why == ""


def _drifted_info_frame(keep: tuple[str, ...] = ()) -> pd.DataFrame:
    """The builder's frame after Snowflake renamed the version keys: GET_PATH returns NULL for a missing
    key (no error), so every row still names its client but the flattened floors are NULL, and RAW_ENTRY
    carries the entry under spellings no alias knows. `keep` names flattened floor columns that survive."""
    return pd.DataFrame([
        {"CLIENT_ID": f.client_id, "CLIENT_APP_ID": f.client_app_id,
         "MIN_SUPPORTED_VERSION": f.min_supported if "MIN" in keep else None,
         "NEARING_EOS_VERSION": f.nearing_eos if "NEARING" in keep else None,
         "RECOMMENDED_VERSION": f.recommended if "RECOMMENDED" in keep else None,
         "RAW_ENTRY": json.dumps({"clientAppId": f.client_app_id, "minimumVersion": f.min_supported,
                                  "latestVersion": f.recommended})}
        for f in _floors()])


@pytest.mark.parametrize("keep", [(), ("RECOMMENDED",), ("NEARING", "RECOMMENDED")])
def test_read_floors_with_no_minimum_anywhere_is_unavailable_not_clean(keep):
    """Key drift: the read succeeds with one row per client and no usable minimum. It used to return those
    floors, so every version read NOT LISTED (or, with only the minimum lost, BELOW RECOMMENDED for
    JDBC 3.13.22) under a green '0 unsupported'."""
    floors, why = cs.read_floors(True, _drifted_info_frame(keep))
    assert floors is None
    assert why == "the function's entries list no minimum supported version; its key names may have changed"
    # an unparseable minimum is no minimum either
    junk = pd.DataFrame([{"CLIENT_APP_ID": "JDBC", "MIN_SUPPORTED_VERSION": "n/a", "RECOMMENDED_VERSION": "4.3.4"}])
    assert cs.read_floors(True, junk)[0] is None
    # one entry with a minimum is enough for a verdict
    assert cs.read_floors(True, _drifted_info_frame(("MIN",)))[0] is not None


def test_a_second_minimum_less_entry_never_erases_a_drivers_minimum():
    go = {"clientId": "GO", "clientAppId": "Go", "minimumSupportedVersion": "1.11.2",
          "minimumNearingEndOfSupportVersion": "1.12.1", "recommendedVersion": "2.2.0"}
    bare = {"clientId": "GO", "clientAppId": "Go", "recommendedVersion": "2.2.0"}
    for entries in ([go, bare], [bare, go]):
        index = cs.floor_index(cs.parse_client_version_info(entries))
        assert index["GO"].min_supported == "1.11.2", entries
    # with it, the web app's Go 1.1.5 stays UNSUPPORTED (Snowflake-run) instead of BELOW RECOMMENDED
    floors = [f for f in _floors() if f.client_app_id != "Go"] + cs.parse_client_version_info([go, bare])
    counts = cs.support_counts(cs.annotate_support(_drivers(), floors))
    assert counts["unsupported_snowflake"] == 2 and counts["unsupported_yours"] == 3


def _drivers_of(rows) -> pd.DataFrame:
    return pd.read_sql_query(_to_sqlite(security_sql.client_drivers(30, "ALL")), _sessions_db(rows))


def test_caption_says_how_many_of_yours_could_not_be_checked():
    # JDBC has no entry at all: its two versions are NOT LISTED, so 'unsupported' covers only the rest
    floors = [f for f in _floors() if f.client_app_id != "JDBC"]
    ann = cs.annotate_support(_drivers(), floors)
    counts = cs.support_counts(ann)
    assert counts["unsupported_yours"] == 2                            # Python 3.10.1, ODBC 3.2.2
    assert (counts["checked_yours"], counts["not_checked_yours"], counts["not_listed_yours"]) == (10, 2, 2)
    text = cs.support_caption(ann)
    assert text.startswith("2 driver versions below Snowflake's supported minimum are yours to upgrade: ")
    assert text.endswith(" 2 of your driver versions could not be checked against a minimum "
                         "(JDBC 3.25.0, JDBC 3.13.22): Snowflake's function lists no minimum for that driver.")


def test_caption_never_reads_clean_when_nothing_of_yours_could_be_checked():
    rows = [("Spark 2.16.0", "spark-submit", ["A"], 3), ("Go 1.1.5", "Snowflake Web App", ["B"], 1),
            ("SQLAPI", None, ["C"], 1)]
    ann = cs.annotate_support(_drivers_of(rows), _floors())
    counts = cs.support_counts(ann)
    assert (counts["checked_yours"], counts["not_checked_yours"]) == (0, 1)
    text = cs.support_caption(ann)
    assert "None of your driver versions in this window is below" not in text
    assert text == ("None of your driver versions could be checked against Snowflake's supported minimum "
                    "(Spark 2.16.0): Snowflake's function lists no minimum for that driver. 1 driver version "
                    "below it is Snowflake-run (Snowflake's own web app or services): no action.")
    # some checked, some not: the clean sentence counts only the checked ones and names the rest
    rows = [("Spark 2.16.0", "spark-submit", ["A"], 3), ("JDBC 4.3.4", "x", ["B"], 2)]
    text = cs.support_caption(cs.annotate_support(_drivers_of(rows), _floors()))
    assert text == ("None of your checked driver versions (1) is below Snowflake's supported minimum. "
                    "1 of your driver versions could not be checked against a minimum (Spark 2.16.0): "
                    "Snowflake's function lists no minimum for that driver.")


# ---------------------------------------------------------------------------
# builders, canary, colours
# ---------------------------------------------------------------------------

def test_client_version_info_builder_uses_the_probe_shape():
    sql = security_sql.client_version_info()
    tree = sqlglot.parse_one(sql, read="snowflake")
    assert tree.named_selects == ["CLIENT_ID", "CLIENT_APP_ID", "MIN_SUPPORTED_VERSION", "NEARING_EOS_VERSION",
                                  "RECOMMENDED_VERSION", "RAW_ENTRY"]
    assert "FLATTEN(INPUT => TRY_PARSE_JSON(SYSTEM$CLIENT_VERSION_INFO()))" in sql
    for key in ("clientId", "clientAppId", "minimumSupportedVersion", "minimumNearingEndOfSupportVersion",
                "recommendedVersion"):
        assert f"GET_PATH(f.value, '{key}')::STRING" in sql, key
    assert "f.value:" not in sql and "ACCOUNT_USAGE" not in sql
    assert not re.search(r"\d+\.\d+\.\d+", sql)                   # never a hard-coded Snowflake version


# A dotted three-part version standing on its own: '3.19.1' inside 'JDBC 3.19.1 today' is one; 'v4.603.0',
# '3.19.1.4' and 'x3.19.1' are not (a word or dot touches it).
_VERSION_IN_TEXT = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])")
# The app's OWN release numbers carry a three-digit minor (4.603.0); Snowflake's client versions never do
# (JDBC 3.25.0, Go 2.2.0, Python connector 4.7.5, .NET 4.1.0), so one is never mistaken for the other.
_APP_RELEASE = re.compile(r"\d+\.\d{3,}\.\d+")


def _hard_coded_versions(source: str) -> list[str]:
    """Every Snowflake-looking version written into a string of this source, anywhere in the text (a
    caption, a help sentence, a constant, an f-string part), except in docstrings, whose examples
    ('3.13.22' -> (3, 13, 22, 0)) document the parser rather than state a floor."""
    import ast
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    found = []
    for node in ast.walk(tree):                      # ast.walk visits f-string (JoinedStr) parts too
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found += [m for m in _VERSION_IN_TEXT.findall(node.value) if not _APP_RELEASE.fullmatch(m)]
    return found


def test_the_version_guard_catches_a_version_inside_a_sentence():
    # the shapes that used to get through: a version in a help sentence, a module constant, an f-string
    assert _hard_coded_versions('HELP = "Unsupported, yours (JDBC 3.19.1 today)."') == ["3.19.1"]
    assert _hard_coded_versions('JDBC_NOTE = "JDBC below 3.19.1 is unsupported"') == ["3.19.1"]
    assert _hard_coded_versions('def f(n):\n    return f"{n} below 3.12.3"') == ["3.12.3"]
    assert _hard_coded_versions('X = "4.3.4"') == ["4.3.4"]
    # not a Snowflake version: docstring examples, the app's own release numbers, version-like fragments
    assert _hard_coded_versions('"""Doc: 3.10.2 > 3.9.1."""\ndef g():\n    """\'3.13.22\' -> (3, 13, 22, 0)"""') == []
    from app.config import APP_VERSION  # the app's own release number, derived
    own =f'A = "{APP_VERSION}"\nB = "rows logged before app 4.599.0"\nC = "(v{APP_VERSION})"'
    assert _hard_coded_versions(own) == []
    assert _hard_coded_versions('D = "10.0.0.1"\nE = "3.19"\nF = "x3.19.1"') == []


def test_no_snowflake_version_is_hard_coded_in_app_code():
    import ast
    for rel in ("app/logic/client_support.py", "app/data/security_sql.py", "app/ui/pages/security.py"):
        source = (_ROOT / rel).read_text(encoding="utf-8")
        assert _hard_coded_versions(source) == [], rel
        tree = ast.parse(source)
        literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not [v for v in literals if re.fullmatch(r"\s*\d+\.\d+(\.\d+)+\s*", v)], rel
    src = (_ROOT / "app" / "logic" / "client_support.py").read_text(encoding="utf-8")
    assert "will stop working" not in src.replace('says an unsupported driver "will stop working"', "")
    page = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "stop working" not in page


def test_canary_and_declared_absence():
    reg = dict(canary.CANARIES)
    assert reg["security.client_version_info"]() == security_sql.client_version_info()
    assert "security.client_version_info" in canary.EXPECTED_GAPS
    assert "security.client_drivers" not in canary.EXPECTED_GAPS


def test_snowflake_run_sql_twin_is_built_from_the_same_constants():
    sql = security_sql.client_drivers(30, "ALL")
    for prefix in cs.SNOWFLAKE_RUN_PROGRAM_PREFIXES:
        assert f"PROGRAM ILIKE '{prefix}%'" in sql
    for driver in cs.SNOWFLAKE_RUN_DRIVERS:
        assert f"'{driver.upper()}'" in sql
    assert f"'{cs.NO_CLIENT_ID}'" in sql


def test_status_colours():
    css = status_colors.status_css
    bad, warn, muted = "#5f1b1b", "#5f3b0b", "#273244"
    assert bad in css("SUPPORT_STATUS", "UNSUPPORTED")
    assert warn in css("SUPPORT_STATUS", "NEARING END OF SUPPORT")
    assert muted in css("SUPPORT_STATUS", "UNSUPPORTED (Snowflake-run)")       # no action: neutral
    assert muted in css("SUPPORT_STATUS", "unavailable")
    assert muted in css("STATUS", "NO VERSION") and muted in css("STATUS", "SNOWFLAKE-RUN")
    assert muted in css("WHO_UPGRADES", "Snowflake-run") and css("WHO_UPGRADES", "Yours") == ""
    assert css("STATUS", "OK") == ""                                            # 'OK' green only here
    assert "#123e2c" in css("SUPPORT_STATUS", "OK")


# ---------------------------------------------------------------------------
# EXECUTED: the real client_drivers SQL in sqlite over the probe's sessions
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 30, 12, 0, 0)

# (CLIENT_APPLICATION_ID, program or None, users, sessions) -- shaped from answers W4 (counts scaled down,
# the ordering that matters kept).
_W4 = [
    ("JDBC 3.25.0", "INFA_DI_Cloud", ["Informatica PRD"], 9),
    ("JDBC 3.25.0", "INFA_IICS", ["Informatica DEV"], 2),
    ("Go 2.0.2", "Go", ["H1", "H2"], 8),
    ("Go 2.0.2", "Snowflake Web App (PAT_FE)", ["H3"], 4),
    ("JDBC 3.13.22", "com.amazonaws.services.glue.P", ["AWS Glue DEV", "AWS Glue PRD", "AWS Glue SAN"], 6),
    ("Go 1.1.5", "Snowflake Web App", ["H4"], 7),
    ("PythonConnector 3.16.0", "PythonConnector", ["AWS Glue PRD"], 5),
    ("Go 2.1.0", "Go", ["CONTROLLER"], 3),
    ("Go 2.1.0", "[ADBC]MashupEngine", ["AP8693"], 2),
    ("PythonConnector 3.10.1", "PythonConnector",
     ["AWS Glue DEV Trexis", "AWS Glue PRD Trexis", "AWS Glue SIT Trexis"], 4),
    ("Go 2.0.0", "terraform-provider-snowflake", ["TERRAFORM_AUTOMATION"], 2),
    ("SQLAPI 2.0.0", None, ["ControlM PRD", "ControlM SAN", "KEBARR1"], 5),
    ("Go 1.6.17", "Snowflake Web App (CNG)", ["H5"], 3),
    ("JavaScript 3.0.0", "cortex_code_cli", ["H6"], 2),
    (None, None, ["H7", "H8"], 3),
    ("Go 1.16.0", "Go", ["STPLATSTREAMLIT98004566893"], 2),
    ("Snowsight", "Snowflake Web App (snowsight_x)", ["H9"], 2),
    ("", None, ["H10"], 1),
    ("ODBC 3.12.1", "posit_workbench_rstudio", ["H11"], 2),
    ("SnowServices Ingress", None, ["H12"], 1),
    ("ODBC 3.2.2", "MashupEngineGateway", ["POWERBI_DEV", "POWERBI_PRD"], 2),
    ("SQLAPI", None, ["H21427", "KEBARR1"], 2),
    ("JDBC 3.13.22", "com.amazonaws.services.glue.P", ["AWS Glue PRD"], 1),   # same combo, more sessions
    # a BEHIND version under a SECOND customer program (live: JavaScript 3.0.0 under cortex_code_cli and
    # cortex_code_sa): program grain would count it twice, so the BEHIND count's DRIVER x VERSION grain is
    # locked by the '6' below (7 at program grain)
    ("Go 2.0.0", "Go", ["H14"], 1),
]


def _regexp_substr(s, pat):
    if s is None:
        return None
    m = re.search(pat, s)
    return m.group(0) if m else None


def _split_part(s, delim, n):
    if s is None:
        return None
    parts = str(s).split(delim)
    return parts[n - 1] if 1 <= n <= len(parts) else ""


def _lpad(s, n, pad):
    if s is None:
        return None
    s = str(s)
    return s[:n] if len(s) >= n else pad * (n - len(s)) + s


def _json_app(env):
    try:
        return json.loads(env).get("APPLICATION") if env else None
    except ValueError:
        return None


def _dateadd(unit, n, ts):
    assert unit == "day"
    return (datetime.fromisoformat(ts) + timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


def _to_sqlite(sql: str) -> str:
    sql = sql.replace("SNOWFLAKE.ACCOUNT_USAGE.SESSIONS", "SESSIONS")
    sql = sql.replace("TRY_PARSE_JSON(CLIENT_ENVIRONMENT):APPLICATION::STRING", "JSON_APP(CLIENT_ENVIRONMENT)")
    sql = sql.replace("CURRENT_TIMESTAMP()", f"'{_NOW:%Y-%m-%d %H:%M:%S}'")
    sql = sql.replace("LISTAGG(DISTINCT USER_NAME, ', ') WITHIN GROUP (ORDER BY USER_NAME)",
                      "GROUP_CONCAT(DISTINCT USER_NAME)")
    sql = sql.replace(" ILIKE ", " LIKE ")                 # sqlite LIKE is ASCII case-insensitive
    # Snowflake's default NULL ordering (DEFAULT_NULL_ORDERING = LAST) treats NULL as the HIGHEST value:
    # NULLs come first on DESC and last on ASC. sqlite treats NULL as the lowest, the reverse. Where the SQL
    # leaves NULL placement implicit, spell out Snowflake's default so the harness sorts as Snowflake does.
    # (A key with no direction sorts ASC: the builder's only one is DRIVER, which is never NULL.)
    sql = re.sub(r"\bDESC\b(?!\s+NULLS)", "DESC NULLS FIRST", sql)
    sql = re.sub(r"\bASC\b(?!\s+NULLS)", "ASC NULLS LAST", sql)
    assert "::" not in sql and "ACCOUNT_USAGE" not in sql
    return sql


def _sessions_db(rows=_W4) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("REGEXP_REPLACE", 3, lambda s, p, r: None if s is None else re.sub(p, r, s))
    con.create_function("REGEXP_SUBSTR", 2, _regexp_substr)
    con.create_function("SPLIT_PART", 3, _split_part)
    con.create_function("LPAD", 3, _lpad)
    con.create_function("LEFT", 2, lambda s, n: None if s is None else str(s)[:n])
    con.create_function("JSON_APP", 1, _json_app)
    con.create_function("DATEADD", 3, _dateadd)
    con.execute("CREATE TABLE SESSIONS (USER_NAME TEXT, CREATED_ON TEXT, CLIENT_APPLICATION_ID TEXT, "
                "CLIENT_ENVIRONMENT TEXT)")
    data = []
    for client_id, program, users, sessions in rows:
        env = json.dumps({"APPLICATION": program, "OS": "Linux"}) if program else None
        for i in range(sessions):
            day = (_NOW - timedelta(days=1 + i % 5)).strftime("%Y-%m-%d %H:%M:%S")
            data.append((users[i % len(users)], day, client_id, env))
    data.append(("OLD", (_NOW - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S"), "JDBC 9.9.9", None))
    con.executemany("INSERT INTO SESSIONS VALUES (?,?,?,?)", data)
    return con


def _drivers(sql: str | None = None) -> pd.DataFrame:
    return pd.read_sql_query(_to_sqlite(sql or security_sql.client_drivers(30, "ALL")), _sessions_db())


def _row(df, driver, version, program=None) -> pd.Series:
    sel = (df["DRIVER"] == driver) & (df["VERSION"] == version)
    if program is not None:
        sel &= df["PROGRAM"] == program
    rows = df[sel]
    assert len(rows) == 1, (driver, version, program, rows)
    return rows.iloc[0]


def test_executed_sqlapi_bare_row_is_no_version_and_never_the_newest():
    df = _drivers()
    assert "JDBC" in set(df["DRIVER"]) and "9.9.9" not in set(df["VERSION"])     # the window still applies
    good = _row(df, "SQLAPI", "2.0.0")
    assert good["STATUS"] == "CURRENT" and good["NEWEST_IN_ACCOUNT"] == "2.0.0"   # was BEHIND, newest '?'
    bare = _row(df, "SQLAPI", "?")
    assert bare["STATUS"] == "NO VERSION" and bare["NEWEST_IN_ACCOUNT"] == "2.0.0"


def test_executed_snowflake_run_rows_are_split_out_of_the_upgrade_signal():
    df = _drivers()
    for version, program in (("1.1.5", "Snowflake Web App"), ("1.6.17", "Snowflake Web App (CNG)"),
                             ("2.0.2", "Snowflake Web App (PAT_FE)")):
        r = _row(df, "Go", version, program)
        assert r["STATUS"] == "SNOWFLAKE-RUN" and r["UPGRADED_BY"] == "SNOWFLAKE", version
        assert r["NEWEST_IN_ACCOUNT"] is None, version
    for driver in ("Snowsight", "SnowServices Ingress"):
        assert _row(df, driver, "?")["STATUS"] == "SNOWFLAKE-RUN"
    # the customer's Go rows compare only with customer Go rows: newest = Power BI's ADBC 2.1.0
    assert _row(df, "Go", "2.0.2", "Go")["STATUS"] == "BEHIND"
    assert _row(df, "Go", "2.0.2", "Go")["NEWEST_IN_ACCOUNT"] == "2.1.0"
    assert _row(df, "Go", "2.1.0", "[ADBC]MashupEngine")["STATUS"] == "CURRENT"
    # parity: the SQL twin and client_support.who_upgrades agree on every row
    for _, r in df.iterrows():
        sql_side = cs.WHO_SNOWFLAKE if r["UPGRADED_BY"] == "SNOWFLAKE" else cs.WHO_YOURS
        assert cs.who_upgrades(r["DRIVER"], r["PROGRAM"]) == sql_side, (r["DRIVER"], r["PROGRAM"])


def test_executed_null_and_blank_client_ids_are_labelled_and_never_green():
    df = _drivers()
    assert "" not in set(df["DRIVER"])
    r = _row(df, cs.NO_CLIENT_ID, "?")
    assert r["STATUS"] == "NO VERSION" and int(r["SESSIONS"]) == 4                # NULL (3) + blank (1)
    assert not ((df["STATUS"] == "CURRENT") & (df["VERSION"] == "?")).any()


def test_executed_rows_are_in_the_labelled_inventory_order():
    df = _drivers()
    assert list(df["DRIVER"]) == sorted(df["DRIVER"])                              # driver, ...
    go = df[df["DRIVER"] == "Go"]["VERSION"].tolist()
    keys = [cs.version_key(v) for v in go]
    assert keys == sorted(keys, reverse=True)                                     # ... newest version first
    sq = df[df["DRIVER"] == "SQLAPI"]["VERSION"].tolist()
    assert sq == ["2.0.0", "?"]                                                    # no version sorts last
    assert cs.INVENTORY_SORT_LABEL == "driver, newest version first"


def test_the_pre_fix_sql_fails_this_harness():
    """The v4.602 builder, verbatim, run through the same harness: it shows each shipped defect, so the
    executed tests above really fail before the fix."""
    old = """
WITH s AS (
    SELECT USER_NAME, CREATED_ON,
        COALESCE(NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                 CLIENT_APPLICATION_ID) AS DRIVER,
        COALESCE(NULLIF(TRIM(REGEXP_SUBSTR(CLIENT_APPLICATION_ID, '[0-9][0-9.]*$')), ''), '?') AS VERSION,
        COALESCE(TRY_PARSE_JSON(CLIENT_ENVIRONMENT):APPLICATION::STRING, '(not reported)') AS PROGRAM
    FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
    WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP()) AND CLIENT_APPLICATION_ID IS NOT NULL
),
keyed AS (
    SELECT s.*,
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 1), ''), '0'), 6, '0') ||
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 2), ''), '0'), 6, '0') ||
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 3), ''), '0'), 6, '0') ||
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 4), ''), '0'), 6, '0') AS VKEY
    FROM s
),
grouped AS (
    SELECT DRIVER, VERSION, PROGRAM, MAX(VKEY) AS VKEY, COUNT(DISTINCT USER_NAME) AS USERS, COUNT(*) AS SESSIONS
    FROM keyed GROUP BY DRIVER, VERSION, PROGRAM
)
SELECT DRIVER, VERSION, PROGRAM, USERS, SESSIONS,
       FIRST_VALUE(VERSION) OVER (PARTITION BY DRIVER ORDER BY VKEY DESC) AS NEWEST_IN_ACCOUNT,
       IFF(VKEY < MAX(VKEY) OVER (PARTITION BY DRIVER), 'BEHIND', 'CURRENT') AS STATUS
FROM grouped
ORDER BY DRIVER, VKEY DESC, SESSIONS DESC
"""
    df = _drivers(old)
    assert _row(df, "SQLAPI", "2.0.0")["STATUS"] == "BEHIND"                      # the false upgrade signal
    assert _row(df, "SQLAPI", "2.0.0")["NEWEST_IN_ACCOUNT"] == "?"
    assert _row(df, "Go", "1.1.5", "Snowflake Web App")["STATUS"] == "BEHIND"    # web app as a task
    assert _row(df, "", "?")["STATUS"] == "CURRENT"                               # blank id: green, empty
    assert cs.NO_CLIENT_ID not in set(df["DRIVER"])                                # NULL ids dropped


def test_the_harness_sees_snowflakes_null_ordering():
    """Snowflake puts NULLs FIRST on DESC by default; sqlite puts them last. Without modelling that, the
    builder with its NULLS LAST dropped ran identically here and only a string lock noticed. It must fail
    the executed checks: on Snowflake bare 'SQLAPI' (a NULL key) would sort first and its NULL would
    become ControlM's NEWEST_IN_ACCOUNT."""
    sql = security_sql.client_drivers(30, "ALL")
    assert sql.count(" NULLS LAST") == 2
    dropped = _drivers(sql.replace(" NULLS LAST", ""))
    assert _row(dropped, "SQLAPI", "2.0.0")["NEWEST_IN_ACCOUNT"] is None
    assert dropped[dropped["DRIVER"] == "SQLAPI"]["VERSION"].tolist() == ["?", "2.0.0"]
    kept = _drivers(sql)
    assert _row(kept, "SQLAPI", "2.0.0")["NEWEST_IN_ACCOUNT"] == "2.0.0"
    assert _to_sqlite("ORDER BY A DESC, B ASC, C DESC NULLS LAST") == (
        "ORDER BY A DESC NULLS FIRST, B ASC NULLS LAST, C DESC NULLS LAST")


def test_executed_a_newer_snowflake_run_version_never_makes_a_customer_behind():
    """STATUS's MAX window is partitioned by UPGRADED_BY too. In the probe's data every Snowflake-run Go is
    older than the customer's newest (2.1.0), so dropping that partition changed no row; the day the web
    app moves to a newer Go, it would make every customer Go row BEHIND (the #34 defect)."""
    base = pd.read_sql_query(_to_sqlite(security_sql.client_drivers(30, "ALL")), _sessions_db())
    rows = [*_W4, ("Go 2.2.0", "Snowflake Web App (CNG)", ["H13"], 2)]
    df = pd.read_sql_query(_to_sqlite(security_sql.client_drivers(30, "ALL")), _sessions_db(rows))
    for program in ("Go", "[ADBC]MashupEngine"):
        r = _row(df, "Go", "2.1.0", program)
        assert (r["STATUS"], r["NEWEST_IN_ACCOUNT"]) == ("CURRENT", "2.1.0"), program
    assert _row(df, "Go", "2.2.0")["STATUS"] == "SNOWFLAKE-RUN"
    assert cs.behind_count(df) == cs.behind_count(base) == 6


# ---------------------------------------------------------------------------
# the join, KPIs and wording on the probe's data
# ---------------------------------------------------------------------------

def test_counts_and_caption_match_the_probe():
    ann = cs.annotate_support(_drivers(), _floors())
    counts = cs.support_counts(ann)
    assert counts["unsupported_yours"] == 3                     # JDBC 3.13.22, Python 3.10.1, ODBC 3.2.2
    assert counts["unsupported_snowflake"] == 2                 # Go 1.1.5, Go 1.6.17 (the web app)
    assert counts["nearing_eos"] == 0
    # yours and below recommended: JDBC 3.25.0, Go 2.0.2 ('Go' program), Python 3.16.0, Go 2.1.0, Go 2.0.0,
    # JavaScript 3.0.0, Go 1.16.0, ODBC 3.12.1
    assert counts["below_recommended"] == 8
    assert cs.support_caption(ann) == (
        "3 driver versions below Snowflake's supported minimum are yours to upgrade: "
        "JDBC 3.13.22 (AWS Glue; minimum 3.19.1, recommended 4.3.4), "
        "Python connector 3.10.1 (used by AWS Glue DEV Trexis, AWS Glue PRD Trexis +1 more; "
        "minimum 3.12.3, recommended 4.7.5), "
        "ODBC 3.2.2 (Power BI gateway; minimum 3.5.0, recommended 3.21.0). "
        "2 more are Snowflake-run (Snowflake's own web app or services): no action.")
    # the in-account STATUS caption: driver x version grain, Snowflake-run excluded
    # JDBC 3.13.22, Go 2.0.2 / 2.0.0 / 1.16.0 (vs Power BI's 2.1.0), Python 3.10.1, ODBC 3.2.2
    assert cs.behind_count(ann) == 6
    assert int((ann["STATUS"] == "BEHIND").sum()) == 7         # Go 2.0.0 is BEHIND under two programs
    assert cs.driver_version_count(ann) == 17                  # Go 2.0.2 / 2.0.0 under two programs count once
    assert cs.driver_family_count(ann) == 8                    # '(no client id)' is not a family
    assert cs.no_client_id_sessions(ann) == 4
    # every version of yours with a version number was checked against a minimum
    assert (counts["checked_yours"], counts["not_checked_yours"], counts["not_listed_yours"]) == (12, 0, 0)


def test_behind_count_is_one_per_driver_version_not_per_program():
    df = pd.DataFrame([
        {"DRIVER": "JavaScript", "VERSION": "3.0.0", "PROGRAM": "cortex_code_cli", "STATUS": "BEHIND"},
        {"DRIVER": "JavaScript", "VERSION": "3.0.0", "PROGRAM": "cortex_code_sa", "STATUS": "BEHIND"},
        {"DRIVER": "JavaScript", "VERSION": "3.1.0", "PROGRAM": "VSCODE_1.40.0", "STATUS": "CURRENT"},
        {"DRIVER": "Go", "VERSION": "1.1.5", "PROGRAM": "Snowflake Web App", "STATUS": "SNOWFLAKE-RUN"},
    ])
    assert cs.behind_count(df) == 1


def test_nearing_and_below_recommended_count_yours_only():
    """Both KPIs say 'Yours'. The probe's data has no Snowflake-run-only version in either band (Go 2.0.2 is
    also run by a customer program), so counting Snowflake's own versions went unnoticed."""
    rows = [("Go 2.0.5", "Snowflake Web App (X)", ["A"], 2), ("Go 1.12.0", "Snowflake Web App (Y)", ["B"], 1),
            ("JDBC 3.25.0", "INFA_IICS", ["C"], 1)]
    df = pd.read_sql_query(_to_sqlite(security_sql.client_drivers(30, "ALL")), _sessions_db(rows))
    ann = cs.annotate_support(df, _floors())
    assert _row(ann, "Go", "2.0.5")["SUPPORT_CODE"] == cs.BELOW_RECOMMENDED    # the case exercises both bands
    assert _row(ann, "Go", "1.12.0")["SUPPORT_CODE"] == cs.NEARING_EOS
    counts = cs.support_counts(ann)
    assert counts["below_recommended"] == 1                                     # JDBC 3.25.0 only
    assert counts["nearing_eos"] == 0


def test_annotate_marks_rows_and_keeps_snowflake_run_neutral():
    ann = cs.annotate_support(_drivers(), _floors())
    web = _row(ann, "Go", "1.1.5", "Snowflake Web App")
    assert web["SUPPORT_CODE"] == cs.UNSUPPORTED and web["SUPPORT_STATUS"] == "UNSUPPORTED (Snowflake-run)"
    assert web["WHO_UPGRADES"] == cs.WHO_SNOWFLAKE and web["MIN_SUPPORTED"] == "1.11.2"
    glue = _row(ann, "JDBC", "3.13.22")
    assert (glue["SUPPORT_STATUS"], glue["WHO_UPGRADES"], glue["CLIENT"]) == ("UNSUPPORTED", "Yours", "AWS Glue")
    assert (glue["MIN_SUPPORTED"], glue["RECOMMENDED"]) == ("3.19.1", "4.3.4")
    assert _row(ann, "SQLAPI", "2.0.0")["SUPPORT_STATUS"] == "OK"
    assert _row(ann, "SQLAPI", "?")["SUPPORT_STATUS"] == "NO VERSION"
    assert _row(ann, "Snowsight", "?")["SUPPORT_STATUS"] == "NOT LISTED (Snowflake-run)"
    nocid = _row(ann, cs.NO_CLIENT_ID, "?")
    assert nocid["SUPPORT_STATUS"] == "NOT LISTED" and pd.isna(nocid["MIN_SUPPORTED"])


def test_support_sort_is_status_then_yours_then_sessions():
    ann = cs.sort_by_support(cs.annotate_support(_drivers(), _floors()))
    top = list(zip(ann["DRIVER"], ann["VERSION"], ann["WHO_UPGRADES"], strict=True))[:5]
    assert top == [("JDBC", "3.13.22", "Yours"), ("PythonConnector", "3.10.1", "Yours"),
                   ("ODBC", "3.2.2", "Yours"), ("Go", "1.1.5", "Snowflake-run"), ("Go", "1.6.17", "Snowflake-run")]
    ranks = [cs.STATUS_RANK[c] for c in ann["SUPPORT_CODE"]]
    assert ranks == sorted(ranks)
    assert cs.SUPPORT_SORT_LABEL == "support status, yours before Snowflake-run, then sessions"
    assert list(cs.display_frame(ann).columns) == list(cs.DISPLAY_COLUMNS)
    assert "UPGRADED_BY" not in cs.display_frame(ann) and "SUPPORT_CODE" not in cs.display_frame(ann)


def test_unavailable_floor_marks_every_row():
    ann = cs.annotate_support(_drivers(), None)
    assert set(ann["SUPPORT_STATUS"]) == {"unavailable"} and ann["MIN_SUPPORTED"].isna().all()
    assert cs.unavailable_caption("boom") == (
        "Snowflake's support floor could not be read (boom). STATUS below only compares each version with "
        "the newest one seen in this account. It is not a support verdict.")


def test_caption_when_nothing_of_yours_is_unsupported():
    rows = [("JDBC 4.3.4", "x", ["A"], 2), ("Go 1.1.5", "Snowflake Web App", ["B"], 1)]
    df = pd.read_sql_query(_to_sqlite(security_sql.client_drivers(30, "ALL")), _sessions_db(rows))
    text = cs.support_caption(cs.annotate_support(df, _floors()))
    assert text == ("None of your driver versions in this window is below Snowflake's supported minimum. "
                    "1 driver version below it is Snowflake-run (Snowflake's own web app or services): no action.")


# ---------------------------------------------------------------------------
# render: _clients_tab with fakes
# ---------------------------------------------------------------------------

class _FakeSt:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def code(self, text, *_a, **_k):
        self.calls.append(("code", str(text)))

    def expander(self, label, *_a, **_k):
        self.calls.append(("expander", str(label)))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


def _ok(df):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, truncated=False, error="", error_kind="",
                           source="stub", fetched_at=None, cache_hit=False)


def _failed(kind, error="boom"):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), truncated=False, error=error,
                           error_kind=kind, source="stub", fetched_at=None, cache_hit=False)


def _info_frame() -> pd.DataFrame:
    return pd.DataFrame([{"CLIENT_ID": f.client_id, "CLIENT_APP_ID": f.client_app_id,
                          "MIN_SUPPORTED_VERSION": f.min_supported, "NEARING_EOS_VERSION": f.nearing_eos,
                          "RECOMMENDED_VERSION": f.recommended, "RAW_ENTRY": "{}"} for f in _floors()])


def _render(monkeypatch, info, drivers=None):
    from app.ui.pages import security as sec
    fake = _FakeSt()
    seen: dict = {"runs": [], "kpis": [], "tables": [], "empty": [], "results": []}
    drivers = drivers if drivers is not None else _ok(_drivers())
    monkeypatch.setattr(sec, "guard", lambda res, *_a, **_k: bool(res.ok) and not res.empty)

    def fake_run(sql, **kw):
        seen["runs"].append(kw)
        return info if "SYSTEM$CLIENT_VERSION_INFO" in sql else drivers

    monkeypatch.setattr(sec, "st", fake)
    monkeypatch.setattr(sec, "run", fake_run)
    monkeypatch.setattr(sec, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(sec, "panel_help", lambda text: seen.setdefault("help", text))
    monkeypatch.setattr(sec, "kpi_row", lambda items, *_a, **_k: seen["kpis"].append(items))
    monkeypatch.setattr(sec, "styled_table", lambda df, **k: seen["tables"].append((df, k)))
    monkeypatch.setattr(sec, "empty_state", lambda kind, msg, *_a, **_k: seen["empty"].append((kind, msg)))
    monkeypatch.setattr(sec, "result_caption", lambda res, *_a, **_k: seen["results"].append(res))
    sec._clients_tab("ALL", 30)
    seen["kpi"] = {k["label"]: k for k in seen["kpis"][0]}
    return fake, seen


def test_render_with_the_floor(monkeypatch):
    fake, seen = _render(monkeypatch, _ok(_info_frame()))
    info_kw = seen["runs"][1]
    assert info_kw["tier"] == "metadata" and info_kw["probe"] is True
    assert info_kw["source"] == "SYSTEM$CLIENT_VERSION_INFO()"
    kpi = seen["kpi"]
    assert kpi["Unsupported, yours to upgrade"]["value"] == "3"
    assert kpi["Unsupported, yours to upgrade"]["severity"] == "bad"
    assert kpi["Unsupported, Snowflake-run"]["value"] == "2"
    assert kpi["Nearing end of support"]["value"] == "0" and kpi["Nearing end of support"]["severity"] == "ok"
    assert kpi["Below recommended"]["value"] == "8"
    assert kpi["Driver+version combos"]["value"] == "17"          # DRIVER x VERSION, not x PROGRAM
    ((table, kw),) = seen["tables"]
    assert kw["sort_label"] == cs.SUPPORT_SORT_LABEL and kw["slug"] == "client-drivers"
    assert list(table.columns) == list(cs.DISPLAY_COLUMNS)
    assert table.iloc[0]["DRIVER"] == "JDBC" and table.iloc[0]["SUPPORT_STATUS"] == "UNSUPPORTED"
    caps = fake.text("caption")
    assert "3 driver versions below Snowflake's supported minimum are yours to upgrade" in caps
    assert "6 driver versions trail the newest version" in caps
    assert "4 sessions reported no client id" in caps
    assert "could not be read" not in caps
    assert len(seen["results"]) == 2                               # both sources are named
    assert "SYSTEM$CLIENT_VERSION_INFO()" in seen["help"] and "(not reported)" in seen["help"]


@pytest.mark.parametrize("kind", ["absent", "unknown_function", "missing_column", "timeout", "other"])
def test_render_when_the_floor_read_fails(monkeypatch, kind):
    fake, seen = _render(monkeypatch, _failed(kind, f"{kind}: no\nfull detail"))
    kpi = seen["kpi"]
    for label in ("Unsupported, yours to upgrade", "Unsupported, Snowflake-run", "Nearing end of support",
                  "Below recommended"):
        assert kpi[label]["value"] == "—" and "severity" not in kpi[label], label
    ((table, kw),) = seen["tables"]
    assert set(table["SUPPORT_STATUS"]) == {"unavailable"}
    assert kw["sort_label"] == cs.INVENTORY_SORT_LABEL
    assert list(table["DRIVER"]) == list(_drivers()["DRIVER"])     # the builder's order stands
    caps = fake.text("caption")
    assert f"Snowflake's support floor could not be read ({kind}: no)." in caps
    assert "yours to upgrade" not in caps
    assert fake.text("expander") == "Support-floor read error" and "full detail" in fake.text("code")
    assert seen["empty"] == [] and len(seen["results"]) == 1       # the inventory still renders


def test_render_when_the_floor_read_returns_nothing(monkeypatch):
    fake, seen = _render(monkeypatch, _ok(pd.DataFrame()))
    assert seen["kpi"]["Unsupported, yours to upgrade"]["value"] == "—"
    assert "returned no client entries" in fake.text("caption")


_SUPPORT_KPIS = ("Unsupported, yours to upgrade", "Unsupported, Snowflake-run", "Nearing end of support",
                 "Below recommended")


@pytest.mark.parametrize("keep", [(), ("RECOMMENDED",)])
def test_render_when_the_floor_keys_drift(monkeypatch, keep):
    """An info frame whose rows name every client but carry no readable minimum (Snowflake renamed the
    keys). It used to render '0' with severity 'ok' on all four KPIs and 'None of your driver versions ...
    is below Snowflake's supported minimum' while JDBC 3.13.22, Python 3.10.1 and ODBC 3.2.2 are below it."""
    fake, seen = _render(monkeypatch, _ok(_drifted_info_frame(keep)))
    for label in _SUPPORT_KPIS:
        assert seen["kpi"][label]["value"] == "—" and "severity" not in seen["kpi"][label], label
    ((table, kw),) = seen["tables"]
    assert set(table["SUPPORT_STATUS"]) == {"unavailable"} and kw["sort_label"] == cs.INVENTORY_SORT_LABEL
    caps = fake.text("caption")
    assert ("Snowflake's support floor could not be read (the function's entries list no minimum supported "
            "version; its key names may have changed).") in caps
    assert "None of your driver versions" not in caps and "yours to upgrade" not in caps


def test_render_qualifies_kpis_when_some_of_yours_are_not_listed(monkeypatch):
    info = _info_frame()
    fake, seen = _render(monkeypatch, _ok(info[info["CLIENT_APP_ID"] != "JDBC"]))
    kpi = seen["kpi"]
    mine = kpi["Unsupported, yours to upgrade"]
    assert (mine["value"], mine["severity"], mine["sub"]) == ("2", "bad", "2 not checked")
    assert "Not counted here: 2 of your driver versions" in mine["help"]
    for label in ("Nearing end of support", "Below recommended"):
        assert kpi[label]["sub"] == "2 not checked", label
    assert kpi["Nearing end of support"]["value"] == "0" and "severity" not in kpi["Nearing end of support"]
    assert kpi["Unsupported, Snowflake-run"]["value"] == "2" and "sub" not in kpi["Unsupported, Snowflake-run"]
    assert "could not be checked against a minimum (JDBC 3.25.0, JDBC 3.13.22)" in fake.text("caption")


def test_render_dashes_kpis_when_none_of_yours_could_be_checked(monkeypatch):
    rows = [("Spark 2.16.0", "spark-submit", ["A"], 3), ("Go 1.1.5", "Snowflake Web App", ["B"], 1)]
    fake, seen = _render(monkeypatch, _ok(_info_frame()), drivers=_ok(_drivers_of(rows)))
    kpi = seen["kpi"]
    for label in ("Unsupported, yours to upgrade", "Nearing end of support", "Below recommended"):
        assert kpi[label]["value"] == "—" and "severity" not in kpi[label], label
    assert kpi["Unsupported, Snowflake-run"]["value"] == "1"
    caps = fake.text("caption")
    assert "None of your driver versions could be checked" in caps
    assert "in this window is below" not in caps


def test_render_when_the_inventory_hits_the_row_cap(monkeypatch):
    """The inventory feed is cut at run()'s cap: every support KPI, the upgrade caption and the BEHIND count
    would be totals over part of it (the oldest versions of the last driver go first), so none is given."""
    capped = SimpleNamespace(**{**vars(_ok(_drivers())), "truncated": True})
    fake, seen = _render(monkeypatch, _ok(_info_frame()), drivers=capped)
    kpi = seen["kpi"]
    for label in _SUPPORT_KPIS:
        assert kpi[label]["value"] == "—" and "severity" not in kpi[label], label
        assert "row cap" in kpi[label]["help"], label
    assert kpi["Driver families"]["value"] == "8+" and kpi["Driver+version combos"]["value"] == "17+"
    caps = fake.text("caption")
    assert cs.capped_caption(len(capped.df)) in caps
    for claim in ("None of your driver versions", "yours to upgrade:", "trail the newest version"):
        assert claim not in caps, claim
    assert "At least 4 sessions reported no client id" in caps
    ((table, _kw),) = seen["tables"]
    assert len(table) == len(capped.df)                          # the rows it has still render


def test_client_drivers_leaves_the_row_cap_to_run():
    """Its own LIMIT 500 sat below run()'s default cap, which keeps a smaller LIMIT as is, so a cut feed
    could never set res.truncated. With no LIMIT of its own, run() fetches cap+1 and can tell."""
    from app.config import DEFAULT_MAX_ROWS
    from app.core.query import _with_row_cap
    sql = security_sql.client_drivers(30, "ALL")
    assert not re.search(r"LIMIT\s+\d+\s*$", sql.strip())
    assert _with_row_cap(sql, DEFAULT_MAX_ROWS).rstrip().endswith(f"LIMIT {DEFAULT_MAX_ROWS + 1}")


def test_logic_module_is_pure():
    src = (_ROOT / "app" / "logic" / "client_support.py").read_text(encoding="utf-8")
    imports = [ln.strip() for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
    assert not [ln for ln in imports if "app.data" in ln or "app.ui" in ln or "streamlit" in ln], imports
    assert "datetime" not in src and "time.time" not in src
