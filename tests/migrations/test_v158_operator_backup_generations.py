"""V158 locks: operator-data backups rotate DAILY into dated generations in OVERWATCH_BAK (Next-Fifty #32).

SP_BACKUP_OPERATOR_TABLES (re-derived from V089, its current definer) clones the 25 operator tables every
day to immutable TRANSIENT ``DBA_MAINT_DB.OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd>`` generations (Sundays also
``_OWBAK_W``, cloned from the D generation, plus V089's own weekly ``<T>_BAK_LAST`` statement), logs row
counts to OPERATOR_BACKUP_LOG (one CLONED row per generation, the Sunday W included), prunes to SETTINGS
BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY (floors 7 / 4) and stamps SOURCE_FRESHNESS_STATE
'OPERATOR_BACKUP_DAILY' only on a clean run. The same file re-derives V_SECURITY_EXCEPTION_QUEUE from V151
with one carve-out for the task's own prune DROPs. Byte-locked to outputs/gen_v158.py.

The proc's load-bearing shapes (the prune candidate query, the Sunday-only W generation, the fail-open
metadata probe, the W row-count arm) are asserted by ``_lock_*`` helpers, and
``test_v158_locks_kill_their_mutations`` proves each helper rejects the mutation it exists to catch.

Wave-2b rework D11 (the backup stays DAILY, trimmed): ONE set-based INFORMATION_SCHEMA probe before the
per-table loop (sources present + generations already taken today; still fail-open), ONE set-based PRUNED
log insert after the prune, and a static statement-budget model (``_budget`` / ``_runs``) that pins a
steady-state weekday at 59 statements (135 on Sundays) and reproduces the untrimmed 107 / 208 on the pre-D11
shape. Python mirrors (``_probe_emulated``, ``_loop_emulated``, ``_pruned_rows_batched``) prove the skip
decisions and the batched rows; the mirrors are tied to the SQL by the exact-text locks.

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


# The daily D and the Sunday-only W generation statements, exactly as the proc carries them (each inside
# its same-day skip IF since the wave-2b rework D11, hence one level deeper than before).
_GEN_D_STMT = ("EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||\n"
               "                                      '_OWBAK_' || :gen_d || ' CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;")
_GEN_W_STMT = ("EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||\n"
               "                                          '_OWBAK_' || :gen_w || ' CLONE DBA_MAINT_DB.OVERWATCH_BAK.' || "
               ":tname ||\n                                          '_OWBAK_' || :gen_d;")
_SUNDAY_OPEN = "                IF (is_sunday) THEN\n"      # the per-table loop's Sunday branch
# The branch's own 16-space close. The leading newline matters: the nested W-skip IF closes with a 20-space
# "END IF;" that contains the bare 16-space text as a substring.
_SUNDAY_CLOSE = "\n                END IF;\n"

# D11: ONE set-based metadata probe before the per-table loop (it was one probe per table). It fails OPEN:
# probe_ok turns TRUE only after the SELECT returned, the handler leaves it FALSE, and every skip below is
# COALESCE(probe_ok AND ..., FALSE) -- so an unreadable INFORMATION_SCHEMA means "clone everything" (V089
# behavior; a missing source then lands as clone_failed), never 25 silent SKIPPED_MISSING rows or 25
# skipped clones.
_PROBE_BLOCK = _norm("""
BEGIN
    SELECT COALESCE(ARRAY_AGG(IFF(TABLE_SCHEMA = 'OVERWATCH', TABLE_NAME, NULL)), ARRAY_CONSTRUCT()),
           COALESCE(ARRAY_AGG(IFF(TABLE_SCHEMA = 'OVERWATCH_BAK' AND RIGHT(TABLE_NAME, 9) = :gen_d,
                                  LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16), NULL)), ARRAY_CONSTRUCT()),
           COALESCE(ARRAY_AGG(IFF(TABLE_SCHEMA = 'OVERWATCH_BAK' AND RIGHT(TABLE_NAME, 9) = :gen_w,
                                  LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16), NULL)), ARRAY_CONSTRUCT())
      INTO :src_present, :have_d, :have_w
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA IN ('OVERWATCH', 'OVERWATCH_BAK')
       AND TABLE_TYPE = 'BASE TABLE'
       AND ((TABLE_SCHEMA = 'OVERWATCH' AND ARRAY_CONTAINS(TABLE_NAME::VARIANT, :tables))
         OR (TABLE_SCHEMA = 'OVERWATCH_BAK' AND REGEXP_LIKE(TABLE_NAME, :prune_re)
             AND RIGHT(TABLE_NAME, 9) IN (:gen_d, :gen_w)));
    probe_ok := TRUE;
EXCEPTION
    WHEN OTHER THEN
        probe_ok := FALSE;
END;""")
_PROBE_OPEN = "    -- ONE set-based metadata probe"
# The three decisions the probe feeds, each a definite TRUE only from an answered probe.
_MISSING_IF = "        IF (COALESCE(probe_ok AND NOT ARRAY_CONTAINS(tname::VARIANT, :src_present), FALSE)) THEN\n"
_SKIP_D_IF = "IF (NOT COALESCE(probe_ok AND ARRAY_CONTAINS(tname::VARIANT, :have_d), FALSE)) THEN"
_SKIP_W_IF = "IF (NOT COALESCE(probe_ok AND ARRAY_CONTAINS(tname::VARIANT, :have_w), FALSE)) THEN"
_D_BLOCK = ("                " + _SKIP_D_IF + "\n                    " + _GEN_D_STMT + "\n"
            "                END IF;\n")
_W_BLOCK = ("                    " + _SKIP_W_IF + "\n                        " + _GEN_W_STMT + "\n"
            "                    END IF;\n")
_LOOP_OPEN, _LOOP_CLOSE = "    FOR i IN 0 TO ARRAY_SIZE(:tables) - 1 DO\n", "\n    END FOR;\n"
_CUR_OPEN, _CUR_CLOSE = "        FOR r IN c_prune DO\n", "\n        END FOR;\n"

# D11: the PRUNED rows are ONE set-based insert after the prune block (it was one INSERT per DROP). Inside
# the cursor loop the DROP only collects its name, after the DROP succeeded and inside the regex guard.
_COLLECT = ("                    pruned := pruned + 1;\n"
            "                    pruned_list := pruned_list || pname || ' ';")
_PRUNE_LOG_BLOCK = _norm("""
IF (pruned > 0) THEN
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION)
        SELECT :run_id, RIGHT(p.VALUE::VARCHAR, 9), LEFT(p.VALUE::VARCHAR, LENGTH(p.VALUE::VARCHAR) - 16),
               p.VALUE::VARCHAR, 'PRUNED'
        FROM TABLE(FLATTEN(INPUT => SPLIT(TRIM(:pruned_list), ' '))) p;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'BackupOperatorTables', 'backup_log_failed', LEFT(:emsg, 2000),
                   'prune log ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): ' || :pruned || ' generation(s) dropped, not logged', CURRENT_ROLE();
    END;
END IF;""")
_PRUNE_LOG_OPEN = "    IF (pruned > 0) THEN\n"
_PRUNE_SCAN_END = "scan stopped, generations not yet dropped are kept', CURRENT_ROLE();\n    END;\n"

# The prune candidate query, whitespace-normalized. Every token is load-bearing: AND -> OR in the QUALIFY
# drops every generation older than today; < -> <= drops today's; ASC / >= / a lost GEN_KIND partition /
# swapped keeps shift which generations survive.
_QUALIFY = ("QUALIFY g.GEN_DAY < :day_ct AND ROW_NUMBER() OVER (PARTITION BY g.BASE_NAME, g.GEN_KIND "
            "ORDER BY g.GEN_DAY DESC) > IFF(g.GEN_KIND = 'D', :keep_d, :keep_w)")
_PRUNE_CANDIDATE = _norm("""
res := (
    SELECT g.TABLE_NAME
    FROM (
        SELECT TABLE_NAME,
               LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16) AS BASE_NAME,
               SUBSTR(TABLE_NAME, LENGTH(TABLE_NAME) - 8, 1) AS GEN_KIND,
               TRY_TO_DATE(RIGHT(TABLE_NAME, 8), 'YYYYMMDD') AS GEN_DAY
        FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_CATALOG = 'DBA_MAINT_DB'
          AND TABLE_SCHEMA = 'OVERWATCH_BAK'
          AND TABLE_TYPE = 'BASE TABLE'
          AND IS_TRANSIENT = 'YES'
          AND REGEXP_LIKE(TABLE_NAME, :prune_re)
    ) g
    WHERE g.GEN_DAY IS NOT NULL
    QUALIFY g.GEN_DAY < :day_ct
        AND ROW_NUMBER() OVER (PARTITION BY g.BASE_NAME, g.GEN_KIND ORDER BY g.GEN_DAY DESC)
            > IFF(g.GEN_KIND = 'D', :keep_d, :keep_w)
    ORDER BY g.TABLE_NAME
);""")
assert _QUALIFY in _PRUNE_CANDIDATE

# The row-count block (comments dropped, whitespace-normalized): the D row every run, the W row inside
# IF (is_sunday) keyed on gen_w, and the freshness ROW_COUNT summed over the D generation only.
_LOG_BLOCK = _norm("""
BEGIN
    INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
        (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, ROW_COUNT, SOURCE_ROW_COUNT, BYTES)
    SELECT :run_id, :gen_d, s.TABLE_NAME, b.TABLE_NAME, 'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES
    FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES b
    JOIN DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES s
      ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'
     AND b.TABLE_NAME = s.TABLE_NAME || '_OWBAK_' || :gen_d
    WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'
      AND REGEXP_LIKE(b.TABLE_NAME, :prune_re);
    IF (is_sunday) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, ROW_COUNT, SOURCE_ROW_COUNT, BYTES)
        SELECT :run_id, :gen_w, s.TABLE_NAME, b.TABLE_NAME, 'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES
        FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES b
        JOIN DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES s
          ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'
         AND b.TABLE_NAME = s.TABLE_NAME || '_OWBAK_' || :gen_w
        WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'
          AND REGEXP_LIKE(b.TABLE_NAME, :prune_re);
    END IF;
    SELECT COALESCE(SUM(ROW_COUNT), 0) INTO :total_rows
      FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
     WHERE RUN_ID = :run_id AND ACTION = 'CLONED' AND GENERATION = :gen_d;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'BackupOperatorTables', 'backup_log_failed', LEFT(:emsg, 2000),
               'row-count log ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): generations taken, counts not logged', CURRENT_ROLE();
END;""")


def _lock_prune_candidate(new: str) -> None:
    cand = _block(new, "res := (", "\n        );")
    assert _QUALIFY in _norm(cand), "the prune QUALIFY connective / window / keep drifted"
    assert _norm(cand) == _PRUNE_CANDIDATE, "the prune candidate query drifted from its golden"


def _lock_sunday_cadence(new: str) -> None:
    sunday = _block(new, _SUNDAY_OPEN, _SUNDAY_CLOSE)
    assert new.count(_GEN_W_STMT) == 1 and _GEN_W_STMT in sunday, "the W generation is cloned on Sundays only"
    assert new.count(_GEN_D_STMT) == 1 and _GEN_D_STMT not in sunday, "the D generation is cloned every day"
    assert new.index(_GEN_D_STMT) < new.index(_SUNDAY_OPEN), "W clones from today's D, so D comes first"


def _lock_probe_fails_open(new: str) -> None:
    probe = _block(new, _PROBE_OPEN, "\n    END;\n")
    assert _norm(_live(probe)) == _PROBE_BLOCK, "the one metadata probe drifted from its golden"
    # probe_ok: FALSE by default, TRUE only after the SELECT returned, left FALSE by the handler
    assert "    probe_ok BOOLEAN DEFAULT FALSE;" in new
    assert new.count("probe_ok := TRUE;") == 1 and new.count("probe_ok := FALSE;") == 1
    assert probe.index("INTO :src_present, :have_d, :have_w") < probe.index("probe_ok := TRUE;") \
        < probe.index("EXCEPTION"), "the success flag must follow the SELECT, inside the guarded block"
    # every decision the probe feeds is a definite TRUE only from an answered probe (fails open)
    assert new.count(_MISSING_IF) == 1, "a source counts as missing only when the probe answered"
    assert new.count(_SKIP_D_IF) == 1 and new.count(_SKIP_W_IF) == 1
    assert new.count("probe_ok AND") == 3, "no other decision may read the probe"


def _lock_single_probe(new: str) -> None:
    """D11: the metadata probe runs ONCE per run, before the per-table loop -- never per table."""
    loop = _block(new, _LOOP_OPEN, _LOOP_CLOSE)
    assert "INFORMATION_SCHEMA" not in loop and "SHOW " not in _live(loop), "the loop must not read metadata"
    # its success path issues the clones and nothing else (the handlers and the rare missing branch aside)
    work = _HANDLER.sub("", loop)
    work = work.replace(_block(work, _MISSING_IF, "        ELSE\n"), "")
    stmts = _stmt_starts(work)
    assert len(stmts) == 3 and all(s.startswith("EXECUTE IMMEDIATE 'CREATE ") for s in stmts), stmts
    assert new[:new.index(_LOOP_OPEN)].count("DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES") == 1
    assert new.index(_PROBE_OPEN) < new.index(_LOOP_OPEN)
    # probe 1 + the D row-count join 2 + the W row-count join 2 + the prune candidates 1
    assert new.count("DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES") == 6


def _lock_generation_skip(new: str) -> None:
    """A same-day re-run skips the no-op CLONE of a generation the probe already saw -- and nothing else."""
    loop = _block(new, _LOOP_OPEN, _LOOP_CLOSE)
    sunday = _block(loop, _SUNDAY_OPEN, _SUNDAY_CLOSE)
    assert loop.count(_D_BLOCK) == 1 and _D_BLOCK not in sunday, "D skip wraps exactly the D clone, every day"
    assert sunday.count(_W_BLOCK) == 1, "W skip wraps exactly the W clone, Sundays only"
    assert loop.index(_D_BLOCK) < loop.index(_SUNDAY_OPEN)
    # the weekly _BAK_LAST pointer refreshes on every Sunday run: after the W skip closes, never inside it
    assert sunday.index(_W_BLOCK) + len(_W_BLOCK) <= sunday.index("EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT")
    # and a skipped clone still counts as done (the table holds today's generation)
    assert loop.index(_SUNDAY_CLOSE) < loop.index("                done := done + 1;")


def _lock_prune_log_set_based(new: str) -> None:
    """D11: ONE PRUNED insert per run, after the prune block; the cursor loop only collects names."""
    cur = _block(new, _CUR_OPEN, _CUR_CLOSE)
    assert "'PRUNED'" not in cur and new.count("'PRUNED'") == 1, "no per-DROP PRUNED insert"
    guard = _block(cur, "IF (REGEXP_LIKE(pname, prune_re)) THEN", "\n            END IF;\n")
    drop = "EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;\n"
    assert cur.count(_COLLECT) == 1 and guard.index(drop) < guard.index(_COLLECT), \
        "a name is collected only after its DROP ran, inside the whole-name regex guard"
    assert guard.index(_COLLECT) < guard.index("EXCEPTION"), "a failed DROP is never collected as PRUNED"
    log = _block(new, _PRUNE_LOG_OPEN, "\n    END IF;\n")
    assert _norm(_live(log)) == _PRUNE_LOG_BLOCK, "the set-based PRUNED insert drifted from its golden"
    # after the prune block (so a scan that stops part-way still logs its DROPs), before the log trim
    assert new.index(_PRUNE_SCAN_END) < new.index(_PRUNE_LOG_OPEN) < new.index("    -- The log trims itself")
    # PRUNE_FAILED stays per row next to its APP_ERROR_LOG row (error path only)
    assert cur.count("'PRUNE_FAILED'") == 1


# ---------------------------------------------------------------------------------------------
# D11 statement budget: a static count of the SQL statements one run issues, per path
# ---------------------------------------------------------------------------------------------
_STMT_START = re.compile(r"(?:(?:SELECT|INSERT INTO|DELETE FROM|MERGE INTO|UPDATE|EXECUTE IMMEDIATE|SHOW|CALL)\b"
                         r"|res := \()")
_HANDLER = re.compile(r"(?ms)^( *)EXCEPTION\n.*?^\1END;\n")


def _stmt_starts(text: str) -> list[str]:
    """SQL statements in a scripting fragment: a DML/DDL keyword at the start of a line, where a SELECT
    only counts when it starts a statement (not the SELECT of an INSERT ... SELECT, a MERGE USING or a
    subquery). Scripting assignments and IF tests are not SQL statements."""
    out: list[str] = []
    prev = ";"
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("--"):
            continue
        if _STMT_START.match(ln) and (not ln.startswith("SELECT") or prev.endswith((";", " THEN", " DO"))
                                       or prev in ("BEGIN", "ELSE")):
            out.append(ln)
        prev = ln.split("--")[0].rstrip()
    return out


def _budget(proc: str, missing_open: str = _MISSING_IF) -> dict[str, int]:
    """Per-path SQL statement counts of one SP_BACKUP_OPERATOR_TABLES run (success paths; the EXCEPTION
    handlers, the SKIPPED_MISSING branch and the backup_incomplete branch are failure/rare paths)."""
    body = _HANDLER.sub("", proc[proc.index("\nBEGIN\n"):])
    loop, cur = _block(body, _LOOP_OPEN, _LOOP_CLOSE), _block(body, _CUR_OPEN, _CUR_CLOSE)
    top = body.replace(loop, "").replace(cur, "")
    top = top.replace(_block(top, "    IF (failed > 0) THEN\n", "\n    END IF;\n"), "")
    prune_log = _block(top, _PRUNE_LOG_OPEN, "\n    END IF;\n") if _PRUNE_LOG_OPEN in top else ""
    top = top.replace(prune_log, "") if prune_log else top
    w_log = _block(top, "        IF (is_sunday) THEN\n", "\n        END IF;\n")
    top = top.replace(w_log, "")
    loop = loop.replace(_block(loop, missing_open, "        ELSE\n"), "")
    sunday = _block(loop, _SUNDAY_OPEN, _SUNDAY_CLOSE)
    return {
        "fixed": 1 + len(_stmt_starts(top)),              # + the task's CALL
        "per_table": len(_stmt_starts(loop.replace(sunday, ""))),
        "per_table_sunday": len(_stmt_starts(sunday)),
        "sunday_log": len(_stmt_starts(w_log)),
        "per_drop": len(_stmt_starts(cur)),
        "prune_log": len(_stmt_starts(prune_log)),
    }


def _runs(b: dict[str, int], n: int = len(TABLES)) -> dict[str, int]:
    """Statements per run: days 1-14 (nothing to prune) and steady state (one D ages out per table per
    day; on Sundays one W per table too)."""
    weekday_first = b["fixed"] + n * b["per_table"]
    weekday = weekday_first + n * b["per_drop"] + b["prune_log"]
    sunday = (b["fixed"] + b["sunday_log"] + n * (b["per_table"] + b["per_table_sunday"])
              + 2 * n * b["per_drop"] + b["prune_log"])
    return {"weekday_first_14_days": weekday_first, "weekday": weekday, "sunday": sunday,
            "week": 6 * weekday + sunday}


def _lock_statement_budget(new: str) -> None:
    b = _budget(new)
    assert b == {"fixed": 8, "per_table": 1, "per_table_sunday": 2, "sunday_log": 1, "per_drop": 1,
                 "prune_log": 1}, b
    assert _runs(b) == {"weekday_first_14_days": 33, "weekday": 59, "sunday": 135, "week": 489}, _runs(b)


def _lock_weekly_generation_logged(new: str) -> None:
    log = _block(new, "    -- Row counts:", "    -- Prune:")
    assert _norm(_live(log)) == _LOG_BLOCK, "the row-count block drifted from its golden"
    assert log.index(":gen_d, s.TABLE_NAME") < log.index("        IF (is_sunday) THEN\n") \
        < log.index(":gen_w, s.TABLE_NAME")


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
    assert new.count(_GEN_D_STMT) == 1 and new.count(_GEN_W_STMT) == 1   # W is cloned from D in OVERWATCH_BAK
    assert "_BAK_' ||" not in new and "_BAK_2" not in new     # never the manual DR token


def test_v158_bak_last_is_v089s_statement_inside_the_sunday_branch():
    base, new = _proc(_V089), _proc(_V158)
    b = base.index("EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE")
    assert _norm(base[b:base.index(";", b) + 1]) == _norm(_V089_BAK_LAST)
    sunday = _block(new, _SUNDAY_OPEN, _SUNDAY_CLOSE)
    # the weekly tier: W is cloned INSIDE IF (is_sunday) and nowhere else (a daily W + the rank prune
    # would collapse 8 weeks of weekly retention to 8 days); D is cloned every day, before the branch
    _lock_sunday_cadence(new)
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
    # a missing source is a logged skip; the metadata probe fails open (V089 behavior): the EXCEPTION
    # handler itself sets present := 1, not just the pre-probe default
    assert "'SKIPPED_MISSING'" in new
    _lock_probe_fails_open(new)


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
    # the fragments above cannot see the connective between them: lock the whole query (AND -> OR in the
    # QUALIFY would drop every generation dated before today and void the 7 / 4 floors)
    _lock_prune_candidate(new)
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
    # _prune below keeps `day < today and rank > keep`: tie the mirror to the SQL's connective and window
    assert _QUALIFY in _norm(new)
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
# D11: one metadata probe, the same-day skip, one PRUNED insert, the statement budget
# ---------------------------------------------------------------------------------------------
def _probe_emulated(rows: list[tuple[str, str]], gen_d: str, gen_w: str) -> tuple[set[str], set[str], set[str]]:
    """Python mirror of the probe's WHERE + the three ARRAY_AGG(IFF(...)) over (TABLE_SCHEMA, TABLE_NAME)
    BASE TABLE rows: (src_present, have_d, have_w)."""
    src: set[str] = set()
    have_d: set[str] = set()
    have_w: set[str] = set()
    for schema, name in rows:
        if schema == "OVERWATCH" and name in TABLES:
            src.add(name)
        elif schema == "OVERWATCH_BAK" and _PRUNE_RX.fullmatch(name) and name[-9:] in (gen_d, gen_w):
            (have_d if name[-9:] == gen_d else have_w).add(name[:len(name) - 16])
    return src, have_d, have_w


def _loop_emulated(probe_ok: bool, src: set[str] | None, have_d: set[str] | None, have_w: set[str] | None,
                   is_sunday: bool) -> tuple[int, int, list[tuple[str, str]]]:
    """Python mirror of the per-table loop's three IF decisions (text-locked by _lock_probe_fails_open and
    _lock_generation_skip). SQL three-valued logic: ARRAY_CONTAINS on a NULL array is NULL, and
    COALESCE(probe_ok AND <NULL>, FALSE) is FALSE -- so an unknown never skips anything."""
    def known(arr: set[str] | None, t: str) -> bool:          # COALESCE(probe_ok AND ARRAY_CONTAINS, FALSE)
        return probe_ok and arr is not None and t in arr

    done = missing = 0
    stmts: list[tuple[str, str]] = []
    for t in TABLES:
        if probe_ok and src is not None and t not in src:     # COALESCE(probe_ok AND NOT ..., FALSE)
            missing += 1
            stmts.append(("SKIPPED_MISSING", t))
            continue
        if not known(have_d, t):
            stmts.append(("CLONE_D", t))
        if is_sunday:
            if not known(have_w, t):
                stmts.append(("CLONE_W", t))
            stmts.append(("BAK_LAST", t))
        done += 1
    return done, missing, stmts


def _kinds(stmts: list[tuple[str, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for kind, _t in stmts:
        out[kind] = out.get(kind, 0) + 1
    return out


def test_v158_one_metadata_probe_before_the_loop_fails_open():
    new = _proc(_V158)
    _lock_single_probe(new)
    _lock_probe_fails_open(new)
    gd, gw = "D20260927", "W20260927"                               # a Sunday
    rows = [("OVERWATCH", t) for t in TABLES] + [
        ("OVERWATCH", "APP_ERROR_LOG"), ("OVERWATCH", "SETTINGS_BAK_LAST"),
        ("OVERWATCH_BAK", "SETTINGS_OWBAK_D20260927"), ("OVERWATCH_BAK", "SETTINGS_OWBAK_D20260926"),
        ("OVERWATCH_BAK", "INCIDENTS_OWBAK_W20260927"), ("OVERWATCH_BAK", "SETTINGS_BAK_20260927"),
        ("OVERWATCH_BAK", "FOO_OWBAK_D20260927"), ("OVERWATCH", "USER_PREFS_OWBAK_D20260927")]
    src, have_d, have_w = _probe_emulated(rows, gd, gw)
    assert src == set(TABLES)                                     # only the 25, never a non-operator table
    assert have_d == {"SETTINGS"} and have_w == {"INCIDENTS"}     # today's, in OVERWATCH_BAK, whole-name only
    # a failed probe: nothing counts as missing or already taken -- all 25 cloned, exactly V089's attempt
    done, missing, stmts = _loop_emulated(False, None, None, None, is_sunday=False)
    assert (done, missing, _kinds(stmts)) == (25, 0, {"CLONE_D": 25})
    done, missing, stmts = _loop_emulated(False, None, None, None, is_sunday=True)
    assert (done, missing, _kinds(stmts)) == (25, 0, {"CLONE_D": 25, "CLONE_W": 25, "BAK_LAST": 25})
    # an answered probe on an install missing the 11 V075 tables: the same skips the per-table probe gave
    v074 = set(TABLES) - set(_V075_ADDITIONS)
    done, missing, stmts = _loop_emulated(True, v074, set(), set(), is_sunday=False)
    assert (done, missing, _kinds(stmts)) == (14, 11, {"CLONE_D": 14, "SKIPPED_MISSING": 11})


def test_v158_same_day_rerun_skips_only_the_no_op_clones():
    new = _proc(_V158)
    _lock_generation_skip(new)
    every = set(TABLES)
    # the normal daily run: every generation is new
    assert _kinds(_loop_emulated(True, every, set(), set(), False)[2]) == {"CLONE_D": 25}
    assert _kinds(_loop_emulated(True, every, set(), set(), True)[2]) == {
        "CLONE_D": 25, "CLONE_W": 25, "BAK_LAST": 25}
    # a same-day re-run (the apply-day tail before 05:10, a manual EXECUTE TASK): no no-op CLONE is issued
    done, _m, stmts = _loop_emulated(True, every, every, set(), False)
    assert done == 25 and stmts == []
    # ... on a Sunday the weekly *_BAK_LAST pointer still refreshes (V089 cadence, every Sunday run)
    done, _m, stmts = _loop_emulated(True, every, every, every, True)
    assert done == 25 and _kinds(stmts) == {"BAK_LAST": 25}
    # ... and a W that failed on the first Sunday run is retried from the D that exists
    done, _m, stmts = _loop_emulated(True, every, every, every - {"INCIDENTS"}, True)
    assert done == 25 and _kinds(stmts) == {"CLONE_W": 1, "BAK_LAST": 25} and ("CLONE_W", "INCIDENTS") in stmts


def _pruned_rows_per_row(names: list[str]) -> list[tuple[str, str, str, str]]:
    """The pre-D11 per-DROP insert: RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16), :pname, 'PRUNED'."""
    return [(n[-9:], n[:len(n) - 16], n, "PRUNED") for n in names]


def _pruned_rows_batched(names: list[str]) -> list[tuple[str, str, str, str]]:
    """The D11 insert: pruned_list := pruned_list || pname || ' ' per DROP, then FLATTEN over
    SPLIT(TRIM(:pruned_list), ' ') with the same RIGHT / LEFT slices on each VALUE."""
    pruned_list = ""
    for n in names:
        pruned_list = pruned_list + n + " "
    return [(v[-9:], v[:len(v) - 16], v, "PRUNED") for v in pruned_list.strip(" ").split(" ")]


def test_v158_pruned_rows_are_one_set_based_insert_with_the_same_rows():
    new = _proc(_V158)
    _lock_prune_log_set_based(new)
    # the same row per dropped generation as the per-DROP insert wrote (cursor order: ORDER BY TABLE_NAME)
    today = dt.date(2026, 9, 27)                                  # a Sunday: one D and one W age out per table
    names: list[str] = []
    for base in TABLES:
        names += [f"{base}_OWBAK_D{(today - dt.timedelta(days=k)):%Y%m%d}" for k in range(15)]   # today + 14
        names += [f"{base}_OWBAK_W{(today - dt.timedelta(weeks=k)):%Y%m%d}" for k in range(9)]   # today + 8
    dropped = sorted(_prune(names, today, 14, 8))
    assert len(dropped) == 25 * 2                                  # the steady-state Sunday: 25 D + 25 W
    assert _pruned_rows_batched(dropped) == _pruned_rows_per_row(dropped)
    # SPLIT on ' ' is lossless: every collected name passed the whole-name regex, which has no whitespace
    assert all(_PRUNE_RX.fullmatch(n) and " " not in n for n in dropped)
    assert not re.search(r"\s", _PRUNE_RX.pattern)
    assert _pruned_rows_batched(["SETTINGS_OWBAK_D20260901"]) == [
        ("D20260901", "SETTINGS", "SETTINGS_OWBAK_D20260901", "PRUNED")]


def test_v158_statement_budget():
    new = _proc(_V158)
    _lock_statement_budget(new)
    # the same model on the pre-D11 shape (the PR #34 text, all four D11 edits reverted) gives the
    # independently counted 107 / 208 / 850 of the cost analysis: the model counts what it claims to
    before = _runs(_budget(_pre_d11(new)))
    assert before == {"weekday_first_14_days": 57, "weekday": 107, "sunday": 208, "week": 850}, before
    # the numbers the header, the SCHEMA_VERSION DESCRIPTION and RUNBOOK section 4 quote are the model's
    assert "A steady-state weekday is 59 statements and a Sunday 135, 489 a week" in _V158
    assert "design: 107 / 208 / 850; V089 ran 26 a week" in _V158
    assert "keep a steady-state day at 59 statements (Sunday 135)" in _V158
    assert "steady-state day is 59 statements (135 on Sundays)" in _norm(_read("RUNBOOK.md"))


def _pre_d11(proc: str) -> str:
    """The PR #34 proc shape rebuilt from the current one: no pre-loop probe and no set-based PRUNED
    insert, the per-table probe back in the loop and the per-DROP PRUNED insert back in the cursor loop."""
    probe = _block(proc, _PROBE_OPEN, "\n    END;\n") + "\n"
    log = _block(proc, _PRUNE_LOG_OPEN, "\n    END IF;\n") + "\n"
    assert proc.count(probe) == 1 and proc.count(log) == 1
    return _PER_ROW_PRUNE_LOG(_PER_TABLE_PROBE(proc.replace(probe, "", 1).replace(log, "", 1)))


# ---------------------------------------------------------------------------------------------
# mutation-kill proofs: each _lock_* helper passes on the real proc and fails on the mutation it guards
# ---------------------------------------------------------------------------------------------
def _swap(old: str, new: str):
    def mutate(text: str) -> str:
        assert text.count(old) == 1, f"mutation anchor drifted (the proof went vacuous): {old!r}"
        return text.replace(old, new, 1)
    return mutate


def _move(line: str, before: str):
    def mutate(text: str) -> str:
        assert text.count(line) == 1 and text.count(before) == 1, "mutation anchor drifted"
        return text.replace(line, "", 1).replace(before, line + before, 1)
    return mutate


def _move_into_sunday(line: str):
    def mutate(text: str) -> str:
        assert text.count(line) == 1 and text.count(_SUNDAY_OPEN) == 1, "mutation anchor drifted"
        return text.replace(line, "", 1).replace(_SUNDAY_OPEN, _SUNDAY_OPEN + line, 1)
    return mutate


# The two D11 reverts, as mutations: the pre-D11 per-table probe back inside the loop, and the pre-D11
# per-DROP PRUNED insert back inside the cursor loop.
_OLD_PROBE = (
    "        BEGIN\n"
    "            SELECT COUNT(*) INTO :present\n"
    "              FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES\n"
    "             WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = :tname\n"
    "               AND TABLE_TYPE = 'BASE TABLE';\n"
    "        EXCEPTION\n"
    "            WHEN OTHER THEN\n"
    "                present := 1;\n"
    "        END;\n")
_OLD_PRUNE_ROW = (
    "                    INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG\n"
    "                        (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION)\n"
    "                    SELECT :run_id, RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16), :pname, 'PRUNED';\n")
_COLLECT_LINE = _COLLECT + "   -- whole-name regex match: no space inside\n"
_PER_TABLE_PROBE = _swap(_MISSING_IF, _OLD_PROBE + _MISSING_IF)
_PER_ROW_PRUNE_LOG = _swap(_COLLECT_LINE, _COLLECT_LINE + _OLD_PRUNE_ROW)


def _move_prune_log_into_the_scan(text: str) -> str:
    """The set-based insert moved inside the prune block (a scan that dies part-way would lose it)."""
    block = _block(text, _PRUNE_LOG_OPEN, "\n    END IF;\n")
    assert text.count(block) == 1 and text.count(_CUR_CLOSE) == 1, "mutation anchor drifted"
    text = text.replace(block, "", 1)
    return text.replace(_CUR_CLOSE, _CUR_CLOSE + block, 1)


def _bak_last_inside_the_w_skip(text: str) -> str:
    """The W skip's END IF moved below _BAK_LAST: a same-day Sunday re-run would stop refreshing it."""
    text = _swap("'_OWBAK_' || :gen_d;\n                    END IF;\n", "'_OWBAK_' || :gen_d;\n")(text)
    return _swap("_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;\n",
                 "_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;\n                    END IF;\n")(text)


def _flag_before_the_select(text: str) -> str:
    """probe_ok set TRUE before the SELECT: a failing probe would leave it TRUE (fails closed)."""
    text = _swap(":gen_w)));\n        probe_ok := TRUE;\n", ":gen_w)));\n")(text)
    return _swap("    BEGIN\n        SELECT COALESCE(ARRAY_AGG(",
                 "    BEGIN\n        probe_ok := TRUE;\n        SELECT COALESCE(ARRAY_AGG(")(text)


_MUTANTS = {
    # #26 the prune QUALIFY (C1: AND -> OR drops every generation dated before today)
    "prune-and-to-or": (_lock_prune_candidate, _swap(
        "QUALIFY g.GEN_DAY < :day_ct\n                AND ROW_NUMBER()",
        "QUALIFY g.GEN_DAY < :day_ct\n                OR ROW_NUMBER()")),
    "prune-lt-to-le": (_lock_prune_candidate, _swap("QUALIFY g.GEN_DAY < :day_ct", "QUALIFY g.GEN_DAY <= :day_ct")),
    "prune-rank-gt-to-ge": (_lock_prune_candidate, _swap("> IFF(g.GEN_KIND = 'D'", ">= IFF(g.GEN_KIND = 'D'")),
    "prune-order-asc": (_lock_prune_candidate, _swap("ORDER BY g.GEN_DAY DESC", "ORDER BY g.GEN_DAY ASC")),
    "prune-partition-loses-kind": (_lock_prune_candidate, _swap(
        "PARTITION BY g.BASE_NAME, g.GEN_KIND", "PARTITION BY g.BASE_NAME")),
    "prune-keeps-swapped": (_lock_prune_candidate, _swap(
        "IFF(g.GEN_KIND = 'D', :keep_d, :keep_w)", "IFF(g.GEN_KIND = 'D', :keep_w, :keep_d)")),
    # #27 the weekly tier (C2: a W generation cloned every day collapses 8 weeks to 8 days); since D11 each
    # clone sits in its same-day skip IF, so the mutants move the whole wrapped statement
    "w-generation-every-day": (_lock_sunday_cadence, _move(_W_BLOCK, _SUNDAY_OPEN)),
    "d-generation-sunday-only": (_lock_sunday_cadence, _move_into_sunday(_D_BLOCK)),
    # #28 + D11 the fail-open probe (a probe that fails closed skips all 25 tables silently)
    "probe-handler-fails-closed": (_lock_probe_fails_open, _swap(
        "        WHEN OTHER THEN\n            probe_ok := FALSE;", "        WHEN OTHER THEN\n            probe_ok := TRUE;")),
    "probe-flag-set-before-the-select": (_lock_probe_fails_open, _flag_before_the_select),
    "probe-default-true": (_lock_probe_fails_open, _swap(
        "    probe_ok BOOLEAN DEFAULT FALSE;", "    probe_ok BOOLEAN DEFAULT TRUE; ")),
    "missing-on-an-unknown": (_lock_probe_fails_open, _swap(
        "NOT ARRAY_CONTAINS(tname::VARIANT, :src_present), FALSE)) THEN",
        "NOT ARRAY_CONTAINS(tname::VARIANT, :src_present), TRUE)) THEN")),
    "probe-reads-views-too": (_lock_probe_fails_open, _swap(
        "         WHERE TABLE_SCHEMA IN ('OVERWATCH', 'OVERWATCH_BAK')\n           AND TABLE_TYPE = 'BASE TABLE'\n",
        "         WHERE TABLE_SCHEMA IN ('OVERWATCH', 'OVERWATCH_BAK')\n")),
    "probe-have-d-any-day": (_lock_probe_fails_open, _swap(
        "'OVERWATCH_BAK' AND RIGHT(TABLE_NAME, 9) = :gen_d,",
        "'OVERWATCH_BAK' AND LEFT(RIGHT(TABLE_NAME, 9), 1) = 'D',")),
    # D11 the single probe (the per-table probe must not come back)
    "per-table-probe-returns": (_lock_single_probe, _PER_TABLE_PROBE),
    # D11 the same-day skip (it must never suppress a clone that is still needed)
    "d-skip-keyed-on-the-source": (_lock_generation_skip, _swap(
        "ARRAY_CONTAINS(tname::VARIANT, :have_d), FALSE)) THEN",
        "ARRAY_CONTAINS(tname::VARIANT, :src_present), FALSE)) THEN")),
    "w-skip-keyed-on-the-d": (_lock_generation_skip, _swap(
        "ARRAY_CONTAINS(tname::VARIANT, :have_w), FALSE)) THEN",
        "ARRAY_CONTAINS(tname::VARIANT, :have_d), FALSE)) THEN")),
    "skip-on-an-unknown": (_lock_generation_skip, _swap(
        "ARRAY_CONTAINS(tname::VARIANT, :have_d), FALSE)) THEN",
        "ARRAY_CONTAINS(tname::VARIANT, :have_d), TRUE)) THEN")),
    "bak-last-inside-the-w-skip": (_lock_generation_skip, _bak_last_inside_the_w_skip),
    # D11 the set-based PRUNED insert
    "prune-log-per-row-again": (_lock_prune_log_set_based, _PER_ROW_PRUNE_LOG),
    "prune-name-not-collected": (_lock_prune_log_set_based, _swap(
        "\n                    pruned_list := pruned_list || pname || ' ';   -- whole-name regex match: no space inside",
        "")),
    "prune-name-collected-before-the-drop": (_lock_prune_log_set_based, _swap(
        "                    EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;\n" + _COLLECT,
        _COLLECT + "\n                    EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;")),
    "prune-log-inside-the-scan": (_lock_prune_log_set_based, _move_prune_log_into_the_scan),
    "prune-log-gate-off-by-one": (_lock_prune_log_set_based, _swap(
        "    IF (pruned > 0) THEN\n", "    IF (pruned > 1) THEN\n")),
    "prune-log-generation-slice": (_lock_prune_log_set_based, _swap(
        "RIGHT(p.VALUE::VARCHAR, 9)", "RIGHT(p.VALUE::VARCHAR, 8)")),
    # D11 the statement budget (either revert, or any new top-level statement, re-inflates the count)
    "budget-per-table-probe": (_lock_statement_budget, _PER_TABLE_PROBE),
    "budget-per-row-prune-log": (_lock_statement_budget, _PER_ROW_PRUNE_LOG),
    "budget-extra-top-level-statement": (_lock_statement_budget, _swap(
        "    -- The log trims itself (SP_PURGE_FACTS is untouched).\n",
        "    -- The log trims itself (SP_PURGE_FACTS is untouched).\n"
        "    SELECT COUNT(*) INTO :total_rows FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG;\n")),
    # #11 the W CLONED row (Sunday-only, keyed on gen_w) and the D-only freshness ROW_COUNT
    "w-log-row-every-day": (_lock_weekly_generation_logged, lambda t: _swap(
        "        END IF;\n        -- The freshness ROW_COUNT", "        -- The freshness ROW_COUNT")(_swap(
            ":prune_re);\n        IF (is_sunday) THEN\n", ":prune_re);\n")(t))),
    "w-log-row-keyed-on-d": (_lock_weekly_generation_logged, _swap(
        "SELECT :run_id, :gen_w, s.TABLE_NAME", "SELECT :run_id, :gen_d, s.TABLE_NAME")),
    "w-log-row-joins-d": (_lock_weekly_generation_logged, _swap(
        "|| '_OWBAK_' || :gen_w\n", "|| '_OWBAK_' || :gen_d\n")),
    "w-log-row-dropped": (_lock_weekly_generation_logged, _swap(
        "SELECT :run_id, :gen_w, s.TABLE_NAME, b.TABLE_NAME, 'CLONED'",
        "SELECT :run_id, :gen_w, s.TABLE_NAME, b.TABLE_NAME, 'SKIPPED'")),
    "freshness-rows-doubled-on-sunday": (_lock_weekly_generation_logged, _swap(
        " AND ACTION = 'CLONED' AND GENERATION = :gen_d;", " AND ACTION = 'CLONED';")),
}


@pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_v158_locks_kill_their_mutations(name):
    lock, mutate = _MUTANTS[name]
    real = _proc(_V158)
    lock(real)                                        # the real proc passes the lock ...
    mutant = mutate(real)
    assert mutant != real
    with pytest.raises((AssertionError, ValueError)):
        lock(mutant)                                  # ... and the mutation it exists to catch fails it


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


def test_v158_weekly_generation_gets_its_own_cloned_row():
    # a Sunday W generation outlives its D twin (the D is pruned on day 15), so it must be named by its own
    # CLONED row -- else, past the daily window, the log points a restore only at dropped tables
    new = _proc(_V158)
    _lock_weekly_generation_logged(new)
    log = _block(new, "    -- Row counts:", "    -- Prune:")
    assert log.count("'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES") == 2
    # the freshness ROW_COUNT stays the daily generation's rows (never doubled on a Sunday)
    assert "WHERE RUN_ID = :run_id AND ACTION = 'CLONED' AND GENERATION = :gen_d;" in log
    rb = _norm(_read("RUNBOOK.md"))
    assert "records every clone, skip and prune" not in rb
    assert "on Sundays the weekly `_W` as its own row" in rb and "The Sunday `*_BAK_LAST` refresh is not logged" in rb


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


_DR_CHOOSER = ("SELECT TABLE_NAME, ROW_COUNT, CREATED FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES "
               "WHERE TABLE_SCHEMA = 'OVERWATCH_BAK' ORDER BY 1;")
_DR_SUSPEND = "ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;"


def _lock_dr_order(section: str, *, replay: str, restore: str, v158: str) -> None:
    """A rebuild restores the operator tables BEFORE V158 is replayed: V158's tail clones whatever the
    tables hold into an immutable generation dated that day and prunes with whatever SETTINGS holds."""
    s = _norm(section)
    assert "V001..V158" not in s, "a full V001..V158 replay before the restore backs up the re-seeded tables"
    assert s.index(replay) < s.index(restore) < s.index(v158), "order: V001..V157 -> restore -> V158"
    assert "SETTINGS first" in s, "SETTINGS first: it carries BACKUP_KEEP_* (the V158 tail prunes with it)"
    assert _DR_CHOOSER in s, "the chooser must survive the loss (INFORMATION_SCHEMA, not the lost log)"
    assert _DR_SUSPEND in s and "never restore from the generation dated the replay day" in s


def test_dr_docs_restore_operator_tables_before_the_v158_replay():
    rb, dep = _read("RUNBOOK.md"), _read("DEPLOYMENT.md")
    step3 = rb[rb.index("3. **Schema gone:**"):rb.index("4. **Bad deploy:**")]
    _lock_dr_order(step3, replay="**V001..V157 only**", restore="Restore the 25 operator tables",
                   v158="Apply V158")
    assert "OPERATOR_BACKUP_LOG` lived in OVERWATCH and is gone" in _norm(step3)
    assert "dated BEFORE the loss" in _norm(step3)
    dropped = dep[dep.index("- **Schema dropped:**"):dep.index("- **App broken after deploy:**")]
    _lock_dr_order(dropped, replay="**V001..V157 only**", restore="Restore the 25 operator tables",
                   v158="Apply V158")
    assert "OPERATOR_BACKUP_LOG` was in OVERWATCH and is gone" in _norm(dropped)
    fr = _read("docs/FULL_REBUILD.md")
    reset = fr[fr.index("- If you factory-reset"):fr.index("## 4. Grants")]
    _lock_dr_order(reset, replay="**stop after V157**", restore="INSERT OVERWRITE INTO SETTINGS",
                   v158="Then apply V158")
    td = _read("snowflake/teardown.sql")
    note = _norm(td[td.index("-- To restore operator data after a factory reset"):td.index("-- C. SHARED")])
    assert note.index("V001..V157 only") < note.index("SETTINGS first") < note.index("THEN apply V158")
    # the migration's own header carries the same order for whoever replays the file
    assert "apply V001..V157, restore the operator tables\n-- (SETTINGS first), THEN this file." in _V158


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
