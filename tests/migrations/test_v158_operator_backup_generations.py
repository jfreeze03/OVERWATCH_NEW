"""V158 locks: operator-data backups rotate DAILY into dated generations in OVERWATCH_BAK (Next-Fifty #32).

SP_BACKUP_OPERATOR_TABLES (re-derived from V089, its current definer) clones the 25 operator tables every
day to immutable TRANSIENT ``DBA_MAINT_DB.OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd>`` generations (Sundays also
``_OWBAK_W``, cloned from the D generation, plus V089's own weekly ``<T>_BAK_LAST`` statement), logs row
counts to OPERATOR_BACKUP_LOG, prunes to SETTINGS BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY (floors 7 / 4) and
stamps SOURCE_FRESHNESS_STATE 'OPERATOR_BACKUP_DAILY' only on a clean run. The same file re-derives
V_SECURITY_EXCEPTION_QUEUE from V151 with one carve-out for the task's own prune DROPs. Byte-locked to
outputs/gen_v158.py.

The wave-tip pins at the bottom (validate 'V001..V158 applied', the DEPLOYMENT/README list lines and the
admin _EXPECTED_MIGRATIONS[158] entry) are written by the wave integrator; they fail until then.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import DEFAULT_SETTINGS

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_NAME = "V158__operator_backup_generations.sql"
_V158 = (_MIG / _NAME).read_text(encoding="utf-8")
_V089 = (_MIG / "V089__backup_transient_clone.sql").read_text(encoding="utf-8")
_V151 = (_MIG / "V151__security_change_risk_identity_policy_drops.sql").read_text(encoding="utf-8")

_PROC_RE = re.compile(
    r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_BACKUP_OPERATOR_TABLES\(\).*?\n\$\$;\n", re.S)
_VIEW_RE = re.compile(
    r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.V_SECURITY_EXCEPTION_QUEUE AS.*?;\n", re.S)

TABLES = (
    "SETTINGS", "COMPANY_SCOPE", "ALERT_CONFIG", "ALERT_EVENTS", "ALERT_AUDIT", "ACTION_QUEUE",
    "SAVINGS_LEDGER", "DEPARTMENT_MAP", "ALERT_ROUTES", "REMEDIATION_LOG", "USER_PREFS",
    "OBJECT_CHANGE_REGISTRY", "WAREHOUSE_CHANGE_REGISTRY", "WAREHOUSE_CONFIG_SNAPSHOT",
    "PIPELINE_SLA_CONFIG", "DAILY_DIGEST", "DEPT_BUDGETS", "INCIDENTS", "INCIDENT_MEMBERS",
    "ACTION_ACTIVITY", "EVIDENCE_LINKS", "ENTITY_CATALOG", "USER_WATCHLIST", "OPTIMIZATION_EXPERIMENTS",
    "SLO_OBJECTIVES",
)
_V075_ADDITIONS = ("WAREHOUSE_CHANGE_REGISTRY", "WAREHOUSE_CONFIG_SNAPSHOT", "DEPT_BUDGETS", "INCIDENTS",
                   "INCIDENT_MEMBERS", "ACTION_ACTIVITY", "EVIDENCE_LINKS", "ENTITY_CATALOG",
                   "USER_WATCHLIST", "OPTIMIZATION_EXPERIMENTS", "SLO_OBJECTIVES")
# Snowflake's REGEXP_LIKE anchors the whole string: re.fullmatch is its Python mirror.
_PRUNE_RX = re.compile("(" + "|".join(TABLES) + ")_OWBAK_[DW][0-9]{8}")
_CARVE_OUT_RE = "DROP TABLE IF EXISTS DBA_MAINT_DB[.]OVERWATCH_BAK[.][A-Z0-9_]+_OWBAK_[DW][0-9]{8}"
_CARVE_OUT = (
    "      -- V158 (Next-Fifty #32): the backup-generation prune of OVERWATCH itself (task-run as SYSTEM,\n"
    "      -- the exact generated DROP only). A human DROP of a backup or any other drop still surfaces.\n"
    "      AND NOT (CHANGE_KIND = 'DESTRUCTIVE'\n"
    "               AND UPPER(COALESCE(USER_NAME, '')) = 'SYSTEM'\n"
    "               AND REGEXP_LIKE(COALESCE(QUERY_PREVIEW, ''),\n"
    f"                   '{_CARVE_OUT_RE}'))"
)
_V089_BAK_LAST = (
    "EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||\n"
    "'_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;"
)


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _norm(s: str) -> str:
    return " ".join(s.split())


def _proc(text: str) -> str:
    (p,) = _PROC_RE.findall(text)
    return p


def _view(text: str) -> str:
    (v,) = _VIEW_RE.findall(text)
    return v


def _live(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))


def _block(text: str, opener: str, closer: str) -> str:
    i = text.index(opener)
    return text[i:text.index(closer, i) + len(closer)]


# ---------------------------------------------------------------------------------------------
# generation + shape
# ---------------------------------------------------------------------------------------------
def test_v158_regenerates_byte_identical(tmp_path):
    out = tmp_path / _NAME
    result = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v158.py")],
                            env={**os.environ, "V158_OUT": str(out)}, cwd=str(_ROOT),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8") == _V158, (
        "V158 drifted from its forward-generation -- edit outputs/gen_v158.py, not the .sql.")


def test_v158_guarded_and_ordered():
    assert "EXCEPTION (-20158, 'V158 requires V157 first - apply migrations in order.')" in _V158
    assert "IF (v < 157) THEN" in _V158
    assert "SELECT 158 AS VERSION" in _V158 and "WHERE VERSION = 158);" in _V158
    assert _V158.count("CREATE OR REPLACE PROCEDURE") == 1
    assert _V158.count("CREATE OR REPLACE VIEW") == 1
    assert "CREATE TASK" not in _V158 and "CREATE WAREHOUSE" not in _V158 and "RESOURCE MONITOR" not in _V158
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_PURGE_FACTS" not in _V158
    steps = [
        "EXCEPTION (-20158",
        "CREATE TRANSIENT SCHEMA IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK COMMENT = "
        "'OVERWATCH operator-data backup generations (V158). Owner-only; never dropped by teardown.';\n",
        "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t",
        "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG (",
        "-- >>> derived:SP_BACKUP_OPERATOR_TABLES  (from V089; daily dated generations in OVERWATCH_BAK + "
        "keep-count prune + row-count log + freshness stamp, V158)\n",
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()",
        "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;",
        "    SET SCHEDULE = 'USING CRON 10 5 * * * America/Chicago';",
        "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR RESUME;",
        "-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V151; + OVERWATCH_BAK backup-prune carve-out, V158)\n",
        "CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE AS",
        "\nEXECUTE TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;\n",
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION",
    ]
    pos = [_V158.index(s) for s in steps]
    assert pos == sorted(pos), "V158 statements are out of the planned order"
    for s in steps[1:]:
        assert _V158.count(s) == 1, s


def test_v158_tail_executes_the_task_and_never_calls_a_proc():
    # the tail is EXECUTE TASK (runs as SYSTEM, inside the carve-out, Central-keyed) -- a CALL would run as
    # the human applying the file (a DR rebuild replay included) and its prunes would not be carved out.
    assert "CALL " not in _V158
    assert _V158.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                   "WHERE VERSION = 158);")
    assert _V158.count("EXECUTE TASK DBA_MAINT_DB.") == 1


def test_v158_lineage_markers_name_the_current_definers():
    from tests.test_proc_lineage import _lineage, _migrations
    rows = {(r["v"], r["proc"]): r for r in _lineage(_migrations())}
    proc = rows[(158, "SP_BACKUP_OPERATOR_TABLES")]
    assert proc["src"] == "marker" and proc["claims"] == [89] and proc["prev"] == 89
    view = rows[(158, "V_SECURITY_EXCEPTION_QUEUE")]
    assert view["src"] == "marker" and view["claims"] == [151] and view["prev"] == 151


# ---------------------------------------------------------------------------------------------
# the proc: V089 carry, generations, TRANSIENT, Central day
# ---------------------------------------------------------------------------------------------
def test_v158_carries_the_v089_prefix_byte_identical():
    base, new = _proc(_V089), _proc(_V158)
    carried = base.split("BEGIN\n", 1)[0]
    assert carried.endswith("    i INT;\n")
    assert new.startswith(carried), "signature / EXECUTE AS OWNER / the 25-name array / decls must be V089's"
    for name in _V075_ADDITIONS:
        assert f"'{name}'" in carried, name
    assert tuple(re.findall(r"'([A-Z_]+)'", carried.split("[", 1)[1].split("]", 1)[0])) == TABLES
    assert "'BackupOperatorTables', 'clone_failed'" in new
    assert "RETURN 'cloned ' || :done || ' operator table(s)" in new


def test_v158_every_dynamic_create_is_transient_and_generations_live_in_overwatch_bak():
    new = _proc(_V158)
    creates = re.findall(r"'CREATE (?:OR REPLACE )?(TRANSIENT )?TABLE (?:IF NOT EXISTS )?([A-Z_.]+)'", new)
    assert len(creates) == 3
    assert all(kind == "TRANSIENT " for kind, _ in creates), "the V089 fix: every clone target is TRANSIENT"
    targets = [t for _, t in creates]
    assert targets.count("DBA_MAINT_DB.OVERWATCH_BAK.") == 2          # the D and W generations
    assert targets.count("DBA_MAINT_DB.OVERWATCH.") == 1              # V089's _BAK_LAST, nothing else
    gen_d = ("EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||\n"
             "                                  '_OWBAK_' || :gen_d || ' CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;")
    gen_w = ("EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||\n"
             "                                      '_OWBAK_' || :gen_w || ' CLONE DBA_MAINT_DB.OVERWATCH_BAK.' || "
             ":tname ||\n                                      '_OWBAK_' || :gen_d;")
    assert new.count(gen_d) == 1 and new.count(gen_w) == 1   # W is cloned from D inside OVERWATCH_BAK
    assert "_BAK_' ||" not in new and "_BAK_2" not in new     # never the manual DR token


def test_v158_bak_last_is_v089s_statement_inside_the_sunday_branch():
    base, new = _proc(_V089), _proc(_V158)
    b = base.index("EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE")
    assert _norm(base[b:base.index(";", b) + 1]) == _norm(_V089_BAK_LAST)
    sunday = _block(new, "                IF (is_sunday) THEN\n", "                END IF;\n")
    n = sunday.index("EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE")
    assert _norm(sunday[n:sunday.index(";", n) + 1]) == _norm(_V089_BAK_LAST)
    assert new.count("_BAK_LAST CLONE") == 1, "_BAK_LAST refreshes only on Sundays (same cadence as V089)"
    # the V089 TRANSIENT rationale travels with it, unchanged (only the indentation moved)
    rationale = base[base.index("            -- V089: TRANSIENT target"):b].rstrip(" ").splitlines()
    assert len(rationale) == 5
    for line in rationale:
        assert "\n" + line.replace("            --", "                    --", 1) + "\n" in sunday, line


def test_v158_generation_naming_cadence_and_timezone():
    new = _proc(_V158)
    for frag in ("day_ct := CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE;",
                 "gen_d := 'D' || TO_CHAR(day_ct, 'YYYYMMDD');",
                 "gen_w := 'W' || TO_CHAR(day_ct, 'YYYYMMDD');",
                 "is_sunday := (DAYOFWEEKISO(day_ct) = 7);",
                 "prune_re := '(' || ARRAY_TO_STRING(:tables, '|') || ')_OWBAK_[DW][0-9]{8}';"):
        assert new.count(frag) == 1, frag
    assert "CURRENT_DATE()" not in new, "the generation day is the Central day, never the session date"
    # a missing source is a logged skip; the metadata probe fails open (V089 behavior)
    assert "'SKIPPED_MISSING'" in new and "present := 1;" in new


# ---------------------------------------------------------------------------------------------
# the prune: scope, regex, rank rule
# ---------------------------------------------------------------------------------------------
def test_v158_prune_is_scoped_to_overwatch_bak_and_double_checked():
    new = _proc(_V158)
    assert new.count("EXECUTE IMMEDIATE 'DROP") == 1
    assert "EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;" in new
    guard = _block(new, "IF (REGEXP_LIKE(pname, prune_re)) THEN", "            END IF;\n")
    assert "EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;" in guard
    cand = _block(new, "res := (", "\n        );")
    for frag in ("TABLE_CATALOG = 'DBA_MAINT_DB'", "TABLE_SCHEMA = 'OVERWATCH_BAK'",
                 "TABLE_TYPE = 'BASE TABLE'", "IS_TRANSIENT = 'YES'", "REGEXP_LIKE(TABLE_NAME, :prune_re)",
                 "WHERE g.GEN_DAY IS NOT NULL", "QUALIFY g.GEN_DAY < :day_ct",
                 "ROW_NUMBER() OVER (PARTITION BY g.BASE_NAME, g.GEN_KIND ORDER BY g.GEN_DAY DESC)",
                 "> IFF(g.GEN_KIND = 'D', :keep_d, :keep_w)"):
        assert frag in cand, frag
    assert "TABLE_SCHEMA = 'OVERWATCH'\n" not in cand and "'OVERWATCH' " not in cand
    assert "keep_d := LEAST(GREATEST(ROUND(keep_d), 7), 60);" in new
    assert "keep_w := LEAST(GREATEST(ROUND(keep_w), 4), 52);" in new
    # the row-count join reads generations in OVERWATCH_BAK against sources in OVERWATCH
    log = _block(new, "'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES", ":prune_re);")
    assert "ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'" in log
    assert "WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'" in log
    # RESULTSET + cursor + FOR (the house cursor-loop shape); the prune and the log are isolated blocks
    assert "LET c_prune CURSOR FOR res;" in new and "FOR r IN c_prune DO" in new
    assert new.count("'backup_prune_failed'") == 2 and "'backup_log_failed'" in new


def _creates_everywhere() -> set[str]:
    from tests.test_teardown_coverage import _CREATE_RE
    names: set[str] = set()
    for path in sorted(_MIG.glob("V[0-9]*.sql")):
        for _kind, name in _CREATE_RE.findall(path.read_text(encoding="utf-8")):
            last = name.upper().rstrip(";").split("(")[0].split(".")[-1]
            if last:
                names.add(last)
    return names


def _manual_bak_names(text: str) -> set[str]:
    return {n.replace("<DATE>", "20260712") for n in
            re.findall(r"\b([A-Z][A-Z0-9_]*_BAK_(?:\d{8}|LAST|<DATE>))", text.upper())}


def test_v158_prune_regex_can_never_match_a_non_backup_table():
    new = _proc(_V158)
    assert "')_OWBAK_[DW][0-9]{8}'" in new and "ARRAY_TO_STRING(:tables, '|')" in new
    for good in ("SETTINGS_OWBAK_D20260101", "SLO_OBJECTIVES_OWBAK_W20251228",
                 "OPTIMIZATION_EXPERIMENTS_OWBAK_D20260924", "INCIDENTS_OWBAK_W20260920"):
        assert _PRUNE_RX.fullmatch(good), good
    for bad in ("SETTINGS", "SETTINGS_BAK_LAST", "SETTINGS_BAK_20260712", "SETTINGS_BAK_20260707",
                "FOO_OWBAK_D20260101", "XSETTINGS_OWBAK_D20260101", "SETTINGS_OWBAK_X20260101",
                "SETTINGS_OWBAK_D2026010", "SETTINGS_OWBAK_D202601011", "SETTINGS_OWBAK_D20260101_X",
                "APP_ERROR_LOG_OWBAK_D20260101", "OPERATOR_BACKUP_LOG"):
        assert not _PRUNE_RX.fullmatch(bad), bad
    # every table name any migration creates (last dotted segment; dynamic 'DBA_MAINT_DB.X.' prefixes skip)
    created = _creates_everywhere()
    assert {"SETTINGS", "OPERATOR_BACKUP_LOG", "ALERT_EVENTS"} <= created, "collision scan is vacuous"
    assert not [n for n in created if _PRUNE_RX.fullmatch(n)]


def test_v158_prune_regex_never_matches_a_manual_dr_clone():
    # collision lock: the manual pre-rebuild clones use <T>_BAK_<yyyymmdd> (teardown B0, rebuild/00,
    # docs/FULL_REBUILD.md) -- a distinct token, so the prune can never drop the owner's DR insurance.
    for rel in ("snowflake/teardown.sql", "snowflake/rebuild/00_backup_operator_data.sql",
                "docs/FULL_REBUILD.md"):
        names = _manual_bak_names(_read(rel))
        assert names, f"{rel}: no _BAK_ names found -- the collision lock went vacuous"
        assert not [n for n in names if _PRUNE_RX.fullmatch(n)], rel


def _clamp(v: float, lo: int, hi: int) -> int:
    return int(min(max(round(v), lo), hi))


def _sql_suffixes(name: str) -> tuple[str, str, str, str]:
    """SQL mirror: LEFT(n, LENGTH(n)-16), SUBSTR(n, LENGTH(n)-8, 1) (1-indexed), RIGHT(n, 8), RIGHT(n, 9)."""
    n = len(name)
    return name[:n - 16], name[n - 9:n - 8], name[-8:], name[-9:]


def _prune(names: list[str], today: dt.date, keep_d: float, keep_w: float) -> set[str]:
    """Python mirror of the candidate query's WHERE + QUALIFY (window over every valid generation,
    today's included; filter afterwards)."""
    kd, kw = _clamp(keep_d, 7, 60), _clamp(keep_w, 4, 52)
    rows = []
    for nm in names:
        if not _PRUNE_RX.fullmatch(nm):
            continue
        base, kind, digits, _gen = _sql_suffixes(nm)
        try:
            day = dt.datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            continue                                   # TRY_TO_DATE -> NULL -> never a candidate
        rows.append((nm, base, kind, day))
    out: set[str] = set()
    for key in {(b, k) for _, b, k, _ in rows}:
        grp = sorted((r for r in rows if (r[1], r[2]) == key), key=lambda r: r[3], reverse=True)
        for rank, (nm, _b, kind, day) in enumerate(grp, start=1):
            if day < today and rank > (kd if kind == "D" else kw):
                out.add(nm)
    return out


def test_v158_prune_rank_rule_emulated():
    new = _proc(_V158)
    for frag in ("LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16) AS BASE_NAME",
                 "SUBSTR(TABLE_NAME, LENGTH(TABLE_NAME) - 8, 1) AS GEN_KIND",
                 "TRY_TO_DATE(RIGHT(TABLE_NAME, 8), 'YYYYMMDD') AS GEN_DAY",
                 "RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16)"):
        assert frag in new, frag
    assert _sql_suffixes("OPTIMIZATION_EXPERIMENTS_OWBAK_D20260924") == (
        "OPTIMIZATION_EXPERIMENTS", "D", "20260924", "D20260924")
    assert _sql_suffixes("INCIDENTS_OWBAK_W20260920") == ("INCIDENTS", "W", "20260920", "W20260920")

    today = dt.date(2026, 9, 27)                            # a Sunday
    names: list[str] = []
    for base in ("USER_PREFS", "OPTIMIZATION_EXPERIMENTS"):
        names += [f"{base}_OWBAK_D{(today - dt.timedelta(days=k)):%Y%m%d}" for k in range(21)]  # today + 20
        names += [f"{base}_OWBAK_W{(today - dt.timedelta(weeks=k)):%Y%m%d}" for k in range(11)]  # today + 10
    names += ["USER_PREFS_BAK_20260712", "USER_PREFS_BAK_LAST", "USER_PREFS_OWBAK_D20261399"]
    pruned = _prune(names, today, 14, 8)
    for base in ("USER_PREFS", "OPTIMIZATION_EXPERIMENTS"):
        d = {n for n in pruned if n.startswith(base + "_OWBAK_D")}
        w = {n for n in pruned if n.startswith(base + "_OWBAK_W")}
        assert d == {f"{base}_OWBAK_D{(today - dt.timedelta(days=k)):%Y%m%d}" for k in range(14, 21)}  # 7 oldest
        assert w == {f"{base}_OWBAK_W{(today - dt.timedelta(weeks=k)):%Y%m%d}" for k in range(8, 11)}  # 3 oldest
    assert not [n for n in pruned if n.endswith(f"{today:%Y%m%d}")], "today's generation is never pruned"
    assert not [n for n in pruned if "_BAK_" in n or n.endswith("1399")]
    # nothing is pruned while there are <= keep generations; the floors defeat a bad setting
    few = [f"SETTINGS_OWBAK_D{(today - dt.timedelta(days=k)):%Y%m%d}" for k in range(14)]
    assert _prune(few, today, 14, 8) == set()
    assert _prune(few, today, 0, 0) == {f"SETTINGS_OWBAK_D{(today - dt.timedelta(days=k)):%Y%m%d}"
                                        for k in range(7, 14)}        # floor 7 keeps a week
    assert _prune(few, today, 999, 999) == set()                       # ceiling 60 > 14 present
    # a paused task: all generations older than today, still only rank > keep is dropped
    stale = [f"SETTINGS_OWBAK_D{(today - dt.timedelta(days=k)):%Y%m%d}" for k in range(30, 50)]
    assert len(_prune(stale, today, 14, 8)) == 6


# ---------------------------------------------------------------------------------------------
# settings, freshness, log, schedule
# ---------------------------------------------------------------------------------------------
def test_v158_settings_seed_matches_defaults_and_editor_bounds():
    merge = _block(_V158, "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t", ";\n")
    assert "WHEN NOT MATCHED THEN INSERT (KEY, VALUE)" in merge and "WHEN MATCHED" not in merge
    for key, default in (("BACKUP_KEEP_DAILY", "14"), ("BACKUP_KEEP_WEEKLY", "8")):
        assert DEFAULT_SETTINGS[key] == default
        assert f"('{key}', '{DEFAULT_SETTINGS[key]!s}')" in merge
    from app.ui.pages import admin
    assert admin._SETTING_EDITORS["BACKUP_KEEP_DAILY"] == (
        admin._NUM, {"min_value": 7.0, "max_value": 60.0, "step": 1.0})
    assert admin._SETTING_EDITORS["BACKUP_KEEP_WEEKLY"] == (
        admin._NUM, {"min_value": 4.0, "max_value": 52.0, "step": 1.0})
    new = _proc(_V158)
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_DAILY', VALUE, NULL))), 14)" in new
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_WEEKLY', VALUE, NULL))), 8)" in new


def test_v158_freshness_stamp_is_daily_named_central_and_held_on_failure():
    new = _proc(_V158)
    assert "'OPERATOR_BACKUP_DAILY' AS SOURCE_NAME" in new
    assert ("IFF(:failed = 0 AND :done > 0, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())"
            "::TIMESTAMP_NTZ, NULL) AS RUN_TS") in new
    assert "LAST_LOAD_TS = COALESCE(s.RUN_TS, t.LAST_LOAD_TS)" in new
    assert "CURRENT_TIMESTAMP()::TIMESTAMP_NTZ" not in new.replace(
        "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ", "")
    # the clone_failed CONTEXT names the source so Admin's stale-source hint links the error to the row
    handler = _block(new, "'BackupOperatorTables', 'clone_failed'", ";")
    assert "' (OPERATOR_BACKUP_DAILY)'" in handler
    assert "'backup_incomplete'" in new
    # DAILY in the name = 30h under every name-based cadence rule
    assert "LIKE '%DAILY%'" in _read("app/data/mart_sql.py")
    assert "LIKE '%DAILY%'" in _read("snowflake/native_alert_templates.sql")
    assert '"DAILY" in n' in _read("app/ui/pages/admin.py")
    assert "DAILY" in _read("app/ui/pages/control_room.py")


def test_v158_log_table_counts_and_self_retention():
    ddl = _block(_V158, "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG (", ");\n")
    for col in ("RUN_ID", "GENERATION", "SOURCE_TABLE", "BACKUP_TABLE", "ACTION", "ROW_COUNT",
                "SOURCE_ROW_COUNT", "BYTES", "DETAIL", "LOGGED_AT"):
        assert f"    {col} " in ddl, col
    new = _proc(_V158)
    for action in ("'CLONED'", "'SKIPPED_MISSING'", "'CLONE_FAILED'", "'PRUNED'", "'PRUNE_FAILED'"):
        assert action in new, action
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG\n     WHERE LOGGED_AT < DATEADD('day', -400," in new
    assert new.count("DELETE FROM") == 1


def test_v158_schedule_moves_in_place_and_task_audit_pins_it():
    sched = ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;\n"
             "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR\n"
             "    SET SCHEDULE = 'USING CRON 10 5 * * * America/Chicago';\n"
             "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR RESUME;\n")
    assert _V158.count(sched) == 1
    audit = _read("snowflake/task_audit.sql")
    assert re.search(r"\('TASK_BACKUP_OPERATOR',\s+'started', 'WH_ALFA_ADMIN', "
                     r"'USING CRON 10 5 \* \* \* America/Chicago', NULL\)", audit)


# ---------------------------------------------------------------------------------------------
# the view carve-out
# ---------------------------------------------------------------------------------------------
def test_v158_view_is_v151_plus_exactly_the_carve_out():
    new, base = _view(_V158), _view(_V151)
    assert new.count(_CARVE_OUT) == 1
    assert new.replace("\n" + _CARVE_OUT, "", 1) == base
    # placed after V151's CHANGE RISK exclusion, before the candidates CTE closes
    i = new.index(_CARVE_OUT)
    assert new.index("'DROP STORAGE LIFECYCLE POLICY %', 'DROP BACKUP POLICY %')))") < i
    assert i < new.index("\n), open_actions AS (")
    # V151's keep list survives (the Part B grid checks it on the live DDL too)
    assert "'DROP MASKING POLICY %'" in new and "LIKE 'TF~_%' ESCAPE '~'" in new


def test_v158_carve_out_regex_matches_only_the_generated_drop():
    rx = re.compile(_CARVE_OUT_RE)
    for base in TABLES:
        for gen in ("D20260101", "W20251228"):
            assert rx.fullmatch(f"DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.{base}_OWBAK_{gen}"), base
    for bad in ("DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT",
                "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SETTINGS_BAK_LAST",
                "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.SETTINGS_BAK_20260712",
                "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SETTINGS_OWBAK_D20260101",
                "DROP TABLE IF EXISTS DBA_MAINT_DB.PUBLIC.X_OWBAK_D20260101",
                "DROP TABLE DBA_MAINT_DB.OVERWATCH_BAK.SETTINGS_OWBAK_D20260101",
                "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.SETTINGS_OWBAK_D20260101; DROP USER X",
                "DROP USER X", "DROP ROLE TF_X", "DROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK"):
        assert not rx.fullmatch(bad), bad
    # the proc's generated DROP (84-88 chars) is exactly what the carve-out keys on, well under the
    # 200-char QUERY_PREVIEW truncation
    longest = "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK." + max(TABLES, key=len) + "_OWBAK_D20260924"
    assert rx.fullmatch(longest) and len(longest) < 200
    assert "'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname" in _proc(_V158)
    assert "UPPER(COALESCE(USER_NAME, '')) = 'SYSTEM'" in _CARVE_OUT     # probe F5 default


# ---------------------------------------------------------------------------------------------
# lockstep: validate row, teardown, docs, loader_chain_check, app predicate
# ---------------------------------------------------------------------------------------------
def test_validate_has_a_central_pinned_backup_freshness_row_outside_the_teeth():
    val = _read("snowflake/validate.sql")
    head, teeth = val.split("EXECUTE IMMEDIATE $$", 1)
    assert "'Operator backups fresh" in head and "OPERATOR_BACKUP_DAILY" not in teeth
    assert ("DATEDIFF('minute', MAX(LAST_LOAD_TS), CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())"
            "::TIMESTAMP_NTZ) / 60.0 <= 30") in head


def test_teardown_keeps_every_backup_and_names_the_new_objects():
    td = _read("snowflake/teardown.sql")
    live = _live(td)
    assert "OPERATOR_BACKUP_LOG" in td and "_OWBAK_" in td and "DBA_MAINT_DB.OVERWATCH_BAK." in td
    assert "_OWBAK_" not in live and "OVERWATCH_BAK" not in live
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG" not in live
    assert "-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG;" in td
    verify = td[td.index("SELECT TABLE_NAME AS REMAINING_OVERWATCH_OBJECT"):]
    assert "'OPERATOR_BACKUP_LOG'" in verify
    assert "INSERT OVERWRITE INTO" in td
    assert "DROP SCHEMA" not in td.upper()


def test_restore_docs_moved_to_insert_overwrite():
    rb, dep = _read("RUNBOOK.md"), _read("DEPLOYMENT.md")
    for doc in (rb, dep):
        assert "INSERT OVERWRITE INTO" in doc and "OVERWATCH_BAK" in doc
        assert "CLONE <T>_BAK_LAST" not in doc and "CLONE <NAME>_BAK_LAST" not in doc
        assert "CLONE <T> AT(OFFSET" not in doc
    assert "TASK_BACKUP_OPERATOR | 05:10 daily" in rb
    assert "BACKUP_KEEP_DAILY 14 (7-60)" in rb and "BACKUP_KEEP_WEEKLY 8 (4-52)" in rb
    assert "INSERT OVERWRITE INTO <T> SELECT * FROM <T> AT(OFFSET => -3600);" in rb
    assert "OVERWATCH_BAK" in _read("docs/FULL_REBUILD.md") and "OVERWATCH_BAK" in _read("FEATURES.md")


def test_loader_chain_check_reads_backup_errors_and_the_30h_rule():
    lcc = _read("snowflake/loader_chain_check.sql")
    step3 = lcc[lcc.index("-- 3)"):lcc.index("-- 4)")]
    assert "'BackupOperatorTables'" in step3
    step4 = lcc[lcc.index("-- 4)"):lcc.index("-- 5)")]
    assert "~30" in step4 and "~26" not in step4


def test_tag_coverage_builders_skip_the_backup_schema():
    from app.data import security_sql
    pred = "NOT (t.TABLE_CATALOG = 'DBA_MAINT_DB' AND t.TABLE_SCHEMA = 'OVERWATCH_BAK')"
    assert pred in security_sql.object_tag_coverage("ALL")
    assert pred in security_sql.untagged_objects("ALL")


def test_v158_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(_V158))
    assert any(s.startswith("CREATE OR REPLACE VIEW") for s in stmts)
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS") for s in stmts)
    for statement in stmts:
        sqlglot.parse(statement, dialect="snowflake")


# ---------------------------------------------------------------------------------------------
# wave-tip pins -- written by the wave integrator (validate floor, DEPLOYMENT/README lists, admin)
# ---------------------------------------------------------------------------------------------
def test_validate_and_docs_track_v158():
    val = _read("snowflake/validate.sql")
    assert "V001..V158 applied" in val and "VERSION BETWEEN 1 AND 158) = 158" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert f"snowflake/migrations/{_NAME}" in _read(rel), rel


def test_v158_in_expected_migrations():
    from app.ui.pages import admin
    assert 158 in admin._EXPECTED_MIGRATIONS
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in admin._EXPECTED_MIGRATIONS[158]
