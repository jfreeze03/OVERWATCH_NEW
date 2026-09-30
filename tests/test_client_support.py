"""Security > Clients: Snowflake's driver support floor (Next-Fifty #34) and the four shipped tab defects.

Pure logic (app/logic/client_support.py): version keys, support status, who upgrades, the tolerant
SYSTEM$CLIENT_VERSION_INFO() parser and the KPI counts/wording. An EXECUTED sqlite harness runs the real
client_drivers SQL over sessions seeded from the owner's 2026-09-29 probe (answers W4/W4c), proving:
SQLAPI 2.0.0 is not BEHIND and bare 'SQLAPI' is NO VERSION (never newest); Snowflake's own web app and
services read SNOWFLAKE-RUN and never set a customer's newest version; NULL/blank client ids are
'(no client id)', never green CURRENT. Render tests drive _clients_tab with fakes: a working floor read
gives 3 yours-to-upgrade / 2 Snowflake-run UNSUPPORTED, a failed one reads 'unavailable' with dashed KPIs.

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


def test_no_snowflake_version_is_hard_coded_in_app_code():
    import ast
    for rel in ("app/logic/client_support.py", "app/data/security_sql.py", "app/ui/pages/security.py"):
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
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
    assert cs.driver_version_count(ann) == 17                  # Go 2.0.2 under two programs counts once
    assert cs.driver_family_count(ann) == 8                    # '(no client id)' is not a family
    assert cs.no_client_id_sessions(ann) == 4


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


def _render(monkeypatch, info):
    from app.ui.pages import security as sec
    fake = _FakeSt()
    seen: dict = {"runs": [], "kpis": [], "tables": [], "empty": [], "results": []}
    drivers = _ok(_drivers())

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


def test_logic_module_is_pure():
    src = (_ROOT / "app" / "logic" / "client_support.py").read_text(encoding="utf-8")
    imports = [ln.strip() for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
    assert not [ln for ln in imports if "app.data" in ln or "app.ui" in ln or "streamlit" in ln], imports
    assert "datetime" not in src and "time.time" not in src
