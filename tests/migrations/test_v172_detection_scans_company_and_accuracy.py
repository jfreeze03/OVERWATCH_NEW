"""Locks for V172 -- the detection scans classify company by the V044 rule and measure what they claim.

Five procs re-derived, each from its current definer, by outputs/gen_v172.py:
  SP_CHANGE_IMPACT_SCAN (V140)      R2-023 company UDF, R2-021 anchored CALL match, R2-025 terminal-attempt TASK
                                    counts, R2-022 settled per-run AFTER credits/call, R1-124 p95 in Hr/Min/Sec
  SP_WAREHOUSE_CHANGE_SCAN (V109)   R1-124 p95 + queue in Hr/Min/Sec
  SP_SCAN_SCHEMA_DRIFT (V133)       R2-024 company UDF in a b (...) wrapper
  SP_SCAN_CLOUD_SVC_ANOMALY (V150)  R1-227 the disabled-rule guard counts ENABLED rows
  SP_ANOMALY_SWEEP (V150)           R2-024 x3, R2-095 pointer, R1-227 rider, CORTEX-NULLIF
plus the bounded repairs R1-R4. STRUCTURE + GENERATION + LOCKS here; tests/migrations/test_v172_harness.py
EXECUTES the HD template, the step-5 credits/call, the collapsed TASK leg and the repairs in sqlite.

Every old/new text below is the TEST's own copy (never imported from the generator): reversing them must give
each base proc back byte-for-byte, and a one-character mutation outside them must not.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import pytest

from tests._source import ROOT, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V172__detection_scans_company_and_accuracy.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V109 = read("snowflake/migrations/V109__warehouse_change_scan_fail_token.sql")
_V133 = read("snowflake/migrations/V133__dq_schema_drift.sql")
_V140 = read("snowflake/migrations/V140__change_impact_exclude_self_procs.sql")
_V150 = read("snowflake/migrations/V150__cloud_svc_anomaly_baseline.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _body(proc: str) -> str:
    return proc[proc.index("$$") + 2:proc.rindex("$$")]


def _norm(text: str) -> str:
    return " ".join(text.split())


_CI, _CI0 = _proc(_MIG, "SP_CHANGE_IMPACT_SCAN()"), _proc(_V140, "SP_CHANGE_IMPACT_SCAN()")
_WH, _WH0 = _proc(_MIG, "SP_WAREHOUSE_CHANGE_SCAN()"), _proc(_V109, "SP_WAREHOUSE_CHANGE_SCAN()")
_DR, _DR0 = _proc(_MIG, "SP_SCAN_SCHEMA_DRIFT()"), _proc(_V133, "SP_SCAN_SCHEMA_DRIFT()")
_CS, _CS0 = _proc(_MIG, "SP_SCAN_CLOUD_SVC_ANOMALY()"), _proc(_V150, "SP_SCAN_CLOUD_SVC_ANOMALY()")
_SW, _SW0 = _proc(_MIG, "SP_ANOMALY_SWEEP()"), _proc(_V150, "SP_ANOMALY_SWEEP()")
_PROCS = {"SP_CHANGE_IMPACT_SCAN": (_CI, _CI0, 140), "SP_WAREHOUSE_CHANGE_SCAN": (_WH, _WH0, 109),
          "SP_SCAN_SCHEMA_DRIFT": (_DR, _DR0, 133), "SP_SCAN_CLOUD_SVC_ANOMALY": (_CS, _CS0, 150),
          "SP_ANOMALY_SWEEP": (_SW, _SW0, 150)}

# ---------------------------------------------------------------------------------------------------------------
# Test-side copies of every delta
# ---------------------------------------------------------------------------------------------------------------
_GUESS = re.compile(r"LIKE\s+'TRXS%'\s*,\s*'Trexis'\s*,\s*'ALFA'", re.I)


def _hd(s: str, nul: str, ind: str) -> str:
    r = f"ROUND({s}, 0, 'HALF_TO_EVEN')"
    w = ind + "     "
    return (f"CASE WHEN {s} IS NULL THEN '{nul}'\n"
            f"{w}WHEN ROUND({s} * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'\n"
            f"{w}WHEN {s} < 1 THEN ROUND({s} * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'\n"
            f"{w}WHEN {s} < 10 THEN TO_VARCHAR(ROUND({s}, 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'\n"
            f"{w}ELSE TRIM(IFF({r} >= 3600, FLOOR({r} / 3600)::INT || 'h ', '')\n"
            f"{w}          || IFF(MOD(FLOOR({r} / 60), 60) > 0, MOD(FLOOR({r} / 60), 60)::INT || 'm ', '')\n"
            f"{w}          || IFF({r} < 3600 AND MOD({r}, 60) > 0, MOD({r}, 60)::INT || 's', ''))\n"
            f"{ind}END")


_P, _C = " " * 15, " " * 18
_HD_NOTE = (f"{_P}-- V172 (R1-124): durations in Hr/Min/Sec, the formulas.humanize_duration twin (HALF_TO_EVEN like\n"
            f"{_P}-- Python round, on fixed-point operands). With the spaced ASCII ' -> ' arrow the app shim\n"
            f"{_P}-- wh_change.humanize_verdict_detail finds nothing to rewrite in the new text.\n")


def _tokens(*pairs: tuple[str, str]) -> str:
    out = ""
    for k, (s, nul) in enumerate(pairs):
        out += f"{_P}|| {_hd(s, nul, _C)}\n"
        if k % 2 == 0:
            out += f"{_P}|| ' -> '\n"
    return out


# SP_CHANGE_IMPACT_SCAN
_OBJ_LINE = "               PROCEDURE_CATALOG || '.' || PROCEDURE_SCHEMA || '.' || PROCEDURE_NAME AS OBJECT_NAME,\n"
_GUESS_1A = "               IFF(PROCEDURE_CATALOG LIKE 'TRXS%', 'Trexis', 'ALFA') AS COMPANY,\n"
_HEAD_1A_OLD = "    USING (\n        SELECT 'PROCEDURE' AS OBJECT_TYPE,\n"
_HEAD_1A_NEW = ("    USING (\n"
                "        SELECT g.OBJECT_TYPE, g.DATABASE_NAME, g.SCHEMA_NAME, g.OBJECT_NAME,\n"
                "               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,  -- V172 "
                "(R2-023): the V044 classification (COMPANY_SCOPE row, then TRXS_, then ALFA%/ADMIN, else UNKNOWN), not "
                "a raw TRXS%/ALFA guess; the UDF on a plain column outside the GROUP BY (V030 shape)\n"
                "               g.CHANGE_SEEN_AT\n"
                "        FROM (\n"
                "        SELECT 'PROCEDURE' AS OBJECT_TYPE,\n")
_GB_OLD, _GB_NEW = "        GROUP BY 1, 2, 3, 4, 5\n    ) s\n", "        GROUP BY 1, 2, 3, 4\n        ) g\n    ) s\n"
_1B_OLD = "                   IFF(DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA') AS COMPANY,\n"
_1B_NEW = ("                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) AS COMPANY,  -- V172 (R2-023): "
           "the V044 classification, not a raw TRXS%/ALFA guess; a plain column, no aggregate\n")
_C3_OLD = "    --    Procedure calls are matched by 'NAME(' in normalized CALL text; a\n"
_C3_NEW = ("    --    Procedure calls are matched by 'CALLNAME(' or '.NAME(' in whitespace-stripped CALL text\n"
           "    --    (V172: was a bare 'NAME(' suffix match, so RUN_NAME co-matched NAME); a\n")


def _pred_old(pad: str) -> str:
    return (f"{pad}AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN\n"
            f"{pad}             REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')) > 0\n")


def _pred_new(pad: str) -> str:
    return (f"{pad}AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN\n"
            f"{pad}              REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0\n"
            f"{pad}     OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN\n"
            f"{pad}                 REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)"
            "   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)\n")


_TERMINAL = """\
          JOIN (
              -- V172 (R2-025): one row per SCHEDULED run. Auto-retry attempts share a SCHEDULED_TIME and collapse
              -- to the terminal attempt BEFORE the join (the object_run_history drill, V101, V126), so runs, fails
              -- and p95 count scheduled runs and a retried-then-succeeded run is no failure. The step-5 credit legs
              -- still read every attempt (retry compute is real spend). The ON predicates below are unchanged.
              SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                     QUERY_START_TIME, COMPLETED_TIME, STATE
              FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
              WHERE SCHEDULED_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                AND STATE IN ('SUCCEEDED', 'FAILED')
              QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                         ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
          ) h
"""
_RAW_TH = "          JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h\n"
_ON_BASE = "            ON h.SCHEDULED_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())\n"
_ON_AFTER = "            ON h.SCHEDULED_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)\n"
_SETTLED = "DATEADD('hour', -8, CURRENT_TIMESTAMP())"
_STEP5_AFTER = [   # (old, new) -- each new text is unique in the V172 proc
    ("           SET AFTER_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.AFTER_CALLS, 0)\n",
     "           SET AFTER_CREDITS_PER_CALL = s.CR_PER_CALL\n"),
    ("              SELECT x.CHANGE_ID, SUM(a.CR) AS TOTAL_CR\n",
     "              -- V172 (R2-022 + R2-025): credits per SETTLED scheduled run. Only runs that started more than 8h\n"
     "              -- ago count (QUERY_ATTRIBUTION_HISTORY lags up to ~8h, the wait the baseline already makes), and\n"
     "              -- numerator and denominator cover the SAME runs: an unattributed settled run adds 0 credits, and\n"
     "              -- a task run's retry attempts add their credits to ONE run (RUN_KEY), so the divisor counts\n"
     "              -- scheduled runs like BASELINE_CALLS. A value is written only once at least one settled run is\n"
     "              -- attributed (HAVING), as for the baseline -- a QAH gap never reads as a false IMPROVED.\n"
     "              SELECT x.CHANGE_ID, SUM(COALESCE(a.CR, 0)) / NULLIF(COUNT(DISTINCT x.RUN_KEY), 0) AS CR_PER_CALL\n"),
    ("                  SELECT r.CHANGE_ID, q.QUERY_ID\n",
     "                  SELECT r.CHANGE_ID, q.QUERY_ID, q.QUERY_ID AS RUN_KEY\n"),
    ("                  SELECT r.CHANGE_ID, h.QUERY_ID\n",
     "                  SELECT r.CHANGE_ID, h.QUERY_ID,\n"
     "                         r.OBJECT_NAME || '|' || TO_VARCHAR(h.SCHEDULED_TIME) AS RUN_KEY\n"),
    ("                   AND q.START_TIME > r.CHANGE_SEEN_AT\n",
     "                   AND q.START_TIME > r.CHANGE_SEEN_AT\n"
     f"                   AND q.START_TIME < {_SETTLED}\n"),
    ("                   AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT\n",
     "                   AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT\n"
     f"                   AND h.QUERY_START_TIME < {_SETTLED}\n"),
    ("              ) x\n              JOIN (\n", "              ) x\n              LEFT JOIN (\n"),
    ("              GROUP BY x.CHANGE_ID\n          ) s\n",
     "              GROUP BY x.CHANGE_ID\n              HAVING COUNT(a.RID) > 0\n          ) s\n"),
]
_CI_P95_OLD = ("               || ' | p95 ' || COALESCE(ROUND(BASELINE_P95_MS / 1000, 1)::VARCHAR, '?') || 's->'\n"
               "               || COALESCE(ROUND(AFTER_P95_MS / 1000, 1)::VARCHAR, '?') || 's'\n")
_CI_P95_NEW = (_HD_NOTE + f"{_P}|| ' | p95 '\n"
               + _tokens(("(BASELINE_P95_MS / 1000)", "?"), ("(AFTER_P95_MS / 1000)", "?")))
# SP_WAREHOUSE_CHANGE_SCAN
_WH_OLD = ("               || ' | p95 ' || COALESCE(BASELINE_P95_S::VARCHAR, '?') || 's->'\n"
           "               || COALESCE(AFTER_P95_S::VARCHAR, '?') || 's'\n"
           "               || ' | queue ' || COALESCE(BASELINE_QUEUED_MIN_PER_DAY::VARCHAR, '0') || '->'\n"
           "               || COALESCE(AFTER_QUEUED_MIN_PER_DAY::VARCHAR, '0') || ' min/d'\n")
_WH_NEW = (_HD_NOTE + f"{_P}|| ' | p95 '\n"
           + _tokens(("(BASELINE_P95_S)", "?"), ("(AFTER_P95_S)", "?"))
           + f"{_P}|| ' | queue '\n"
           + _tokens(("(COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60)", "0s"),
                     ("(COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60)", "0s"))
           + f"{_P}|| '/day'\n")
# the b (...) wrapper (R2-024)
_B_SEL = "SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY"
_B_COLS = "(RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)"
_NOTE = ("  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); "
         "the b (...) wrapper as in SP_ALERT_SCAN")


def _wrap(ind: str, sel: str, guess: str, arg: str, key: str) -> list[tuple[str, str]]:
    head_old = f"{ind}SELECT {sel},\n{ind}       IFF({guess} LIKE 'TRXS%', 'Trexis', 'ALFA'),\n"
    head_new = (f"{ind}{_B_SEL}\n{ind}FROM (\n{ind}SELECT {sel},\n"
                f"{ind}       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE({arg}),{_NOTE}\n")
    tail_old = (f"{ind}WHERE NOT EXISTS (\n{ind}    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
                f"{ind}    WHERE e.DEDUPE_KEY = {key}\n{ind});\n")
    tail_new = (f"\n{ind}) b {_B_COLS}\n{ind}WHERE NOT EXISTS (\n{ind}    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
                f"{ind}    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n{ind});\n")
    return [(head_old, head_new), (tail_old, tail_new)]


_KEY_DRIFT = "cfg.RULE_ID || '|' || a.FQN || '|' || TO_VARCHAR(CURRENT_DATE())"
_KEY_DT = ("c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||\n"
           "                  '.' || d.NAME || '|' || TO_VARCHAR(CURRENT_DATE())")
_KEY_VOL = ("c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL ||\n"
            "                  '|' || TO_VARCHAR(CURRENT_DATE())")
_KEY_DQ = "c.RULE_ID || '|' || s.FQN || '|' || TO_VARCHAR(s.DAY)"
_DRIFT_EDITS = _wrap("    ", "cfg.RULE_ID", "SPLIT_PART(a.FQN, '.', 1)", "SPLIT_PART(a.FQN, '.', 1)", _KEY_DRIFT)
_DT_EDITS = _wrap("        ", "c.RULE_ID", "d.DATABASE_NAME", "d.DATABASE_NAME", _KEY_DT)
_VOL_EDITS = _wrap("        ", "c.RULE_ID", "v.DB", "v.DB", _KEY_VOL)
_DQ_EDITS = _wrap("        ", "c.RULE_ID", "s.DB", "s.DB", _KEY_DQ)
# SP_SCAN_CLOUD_SVC_ANOMALY (R1-227)
_CS_EDITS = [
    ("    cs_floor FLOAT DEFAULT 1.0;   -- CS credits/day floor: below this, a spike is not worth paging\n",
     "    cs_floor FLOAT DEFAULT 1.0;   -- CS credits/day floor: below this, a spike is not worth paging\n"
     "    enabled_cnt INT;\n"),
    ("    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr\n",
     "    -- V172 (R1-227): gate on the ENABLED row count like SP_SCAN_SCHEMA_DRIFT / SP_SCAN_RECON_ERRORS. V150\n"
     "    -- tested :zthr IS NULL after COALESCE(.., 3.5), which never fired, so a disabled or deleted rule still\n"
     "    -- scanned (at 3.5, not the tuned threshold) and SP_NOTIFY_WEBHOOK delivered what it booked.\n"
     "    SELECT COUNT(*), COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :enabled_cnt, :zthr\n"),
    ("    IF (:zthr IS NULL) THEN\n", "    IF (:enabled_cnt = 0) THEN\n"),
]
# SP_ANOMALY_SWEEP: the rider, the pointer, CORTEX-NULLIF
_RIDER = ("      -- V172 (R1-227 rider): a disabled or deleted COST_ANOMALY_SWEEP rule books nothing. The threshold read\n"
          "      -- above COALESCEs to 3.5 and never gated, so turning the rule off changed nothing. Not an early\n"
          "      -- RETURN: the DT, drift, creep, volume, DQ and cloud-services arms below still run.\n"
          "      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n"
          "                  WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED)\n")
_PTR_OLD = "                   '. Breakdown: Admin > Org spend.',\n"
_PTR_NEW = "                   '. Breakdown: Cost Intelligence > Contract & Forecast.',\n"
_MODEL_RE_LITERAL = "[a-z0-9][a-z0-9.-]{1,60}"
_CORTEX_OLD = ("        SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')\n"
               "          INTO :ai_model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;\n")
_CORTEX_NEW = ("        -- V172 CORTEX-NULLIF - CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a\n"
               "        -- valid name else the default): a blank, padded, mixed-case or invalid stored value no longer\n"
               "        -- reaches COMPLETE (the V171 digest read, same literal)\n"
               f"        SELECT IFF(RLIKE(cm, '{_MODEL_RE_LITERAL}'), cm, 'llama3.1-8b')\n"
               "          INTO :ai_model\n"
               "        FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm\n"
               "              FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);\n")
_MARKERS = {
    "SP_CHANGE_IMPACT_SCAN": "-- >>> derived:SP_CHANGE_IMPACT_SCAN  (from V140; ",
    "SP_WAREHOUSE_CHANGE_SCAN": "-- >>> derived:SP_WAREHOUSE_CHANGE_SCAN  (from V109; ",
    "SP_SCAN_SCHEMA_DRIFT": "-- >>> derived:SP_SCAN_SCHEMA_DRIFT  (from V133; ",
    "SP_SCAN_CLOUD_SVC_ANOMALY": "-- >>> derived:SP_SCAN_CLOUD_SVC_ANOMALY  (from V150; ",
    "SP_ANOMALY_SWEEP": "-- >>> derived:SP_ANOMALY_SWEEP  (from V150; ",
}


def _rev(text: str, new: str, old: str, n: int = 1) -> str:
    assert text.count(new) == n, (new[:70], text.count(new))
    return text.replace(new, old)


def _reverse_ci(p: str) -> str:
    p = _rev(p, _CI_P95_NEW, _CI_P95_OLD)
    for old, new in reversed(_STEP5_AFTER):
        p = _rev(p, new, old)
    p = _rev(p, _TERMINAL + _ON_AFTER, _RAW_TH + _ON_AFTER)
    p = _rev(p, _TERMINAL + _ON_BASE, _RAW_TH + _ON_BASE)
    p = _rev(p, _pred_new(" " * 19), _pred_old(" " * 19), n=2)
    p = _rev(p, _pred_new(" " * 11), _pred_old(" " * 11), n=2)
    p = _rev(p, _C3_NEW, _C3_OLD)
    p = _rev(p, _1B_NEW, _1B_OLD)
    p = _rev(p, _GB_NEW, _GB_OLD)
    p = _rev(p, _OBJ_LINE, _OBJ_LINE + _GUESS_1A)
    return _rev(p, _HEAD_1A_NEW, _HEAD_1A_OLD)


def _unwrap(p: str, edits: list[tuple[str, str]]) -> str:
    """Reverse one b (...) wrapper: its head is unique (the UDF line); its tail is the same text in every wrapped
    arm, so it is reversed at the first occurrence AFTER that arm's head."""
    (head_old, head_new), (tail_old, tail_new) = edits
    p = _rev(p, head_new, head_old)
    j = p.index(tail_new, p.index(head_old))
    return p[:j] + tail_old + p[j + len(tail_new):]


def _reverse_sw(p: str) -> str:
    p = _rev(p, _CORTEX_NEW, _CORTEX_OLD)
    for edits in (_DQ_EDITS, _VOL_EDITS, _DT_EDITS):
        p = _unwrap(p, edits)
    p = _rev(p, _PTR_NEW, _PTR_OLD)
    return _rev(p, _RIDER, "")


def _reverse_simple(p: str, edits: list[tuple[str, str]]) -> str:
    for old, new in reversed(edits):
        p = _rev(p, new, old)
    return p


_REVERSE = {
    "SP_CHANGE_IMPACT_SCAN": _reverse_ci,
    "SP_WAREHOUSE_CHANGE_SCAN": lambda p: _rev(p, _WH_NEW, _WH_OLD),
    "SP_SCAN_SCHEMA_DRIFT": lambda p: _reverse_simple(p, _DRIFT_EDITS),
    "SP_SCAN_CLOUD_SVC_ANOMALY": lambda p: _reverse_simple(p, _CS_EDITS),
    "SP_ANOMALY_SWEEP": _reverse_sw,
}


# ---------------------------------------------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------------------------------------------
_EXTRA_ENVS = ("V172_OUT", "PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _EXTRA_ENVS}
    env.update(extra)
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v172.py")], env=env, cwd=tmp_path,
                          capture_output=True, text=True)


def _extras(tmp_path: Path) -> dict[str, str]:
    names = {"preflight": "PREFLIGHT_OUT", "part_b": "PART_B_OUT", "repair": "REPAIR_OUT"}
    paths = {k: tmp_path / f"{k}.sql" for k in names}
    result = _run_gen(tmp_path, V172_OUT=str(tmp_path / "m.sql"), **{v: str(paths[k]) for k, v in names.items()})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG       # the extras never change the migration
    return {k: p.read_text(encoding="utf-8") for k, p in paths.items()}


def test_v172_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V172_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V172 drifted from its forward-generation -- edit outputs/gen_v172.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]      # extras only on request
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout and "REPAIRS" not in result.stdout


def test_v172_generator_reads_only_its_bases_and_never_imports_app():
    gen = read("outputs/gen_v172.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == [
        "V109__warehouse_change_scan_fail_token.sql", "V133__dq_schema_drift.sql",
        "V140__change_impact_exclude_self_procs.sql", "V150__cloud_svc_anomaly_baseline.sql"]
    assert gen.count(".read_text(") == 4
    assert not re.search(r"^\s*(?:from|import)\s+app\b", gen, re.M)
    assert "def _swap(text: str, old: str, new: str, label: str, n: int = 1)" in gen


@pytest.mark.parametrize("which", ["preflight", "part_b", "repair"])
def test_v172_preflight_part_b_and_repairs_are_read_only_and_parse(tmp_path, which):
    sql = _extras(tmp_path)[which]
    code = _strip_noise(sql)                        # comments + strings out: the optional statements are comments
    stmts = [s.strip() for s in code.split(";") if s.strip()]
    if which == "preflight":
        assert not re.search(r"\bALTER\b", code)
    else:                                           # Central first (correction 5): days key by session DATE()
        assert stmts[0] == "ALTER SESSION SET TIMEZONE =" and sql.count("ALTER SESSION SET TIMEZONE") == 1
        assert "ALTER SESSION SET TIMEZONE = 'America/Chicago';" in sql
        code = code.replace("ALTER SESSION SET TIMEZONE =", "", 1)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                   "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    body = sql.replace("ALTER SESSION SET TIMEZONE = 'America/Chicago';", "")
    parsed = [p for p in sqlglot.parse(body, dialect="snowflake") if p is not None]
    assert len(parsed) == {"preflight": 6, "part_b": 4, "repair": 4}[which]
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]


def test_v172_repairs_block_keeps_its_two_optional_writes_commented(tmp_path):
    rep = _extras(tmp_path)["repair"]
    assert "-- CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN();" in rep
    assert "-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e" in rep
    assert "RESOLUTION_KIND = 'EXPECTED'" in rep
    for grid in ("-- R172.0 ", "-- R172.1 ", "-- R172.2 ", "-- R172.3 ", "-- R172.4 "):
        assert rep.count(grid) == 1, grid


def test_v172_preflight_carries_the_template_and_repair_text(tmp_path):
    """What the owner previews is what V172 runs: P172.1 renders the scans' own HD template, and P172.2-P172.5 read
    through the repairs' own derived tables."""
    from app.logic.formulas import humanize_duration
    pre = _extras(tmp_path)["preflight"]
    assert _hd("(t.S)", "?", "       ") in pre
    rows = re.findall(r"\('([^']+)', ([0-9.]+|NULL), '([^']*)'\)", pre)
    assert len(rows) >= 12
    for lbl, val, want in rows:
        assert want == ("?" if val == "NULL" else humanize_duration(float(val), "s")), (lbl, want)
    registry_map = _between(_repair("-- R1 (R2-023)"), "      SELECT d.DATABASE_NAME", "  ) m\n")
    suffix_join = _between(_repair("-- R3 (R2-021)"), "      JOIN SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES p\n",
                           "      WHERE r.OBJECT_TYPE")
    terminal_30 = _between(_repair("-- R4 (R2-025)"), "      JOIN (SELECT DATABASE_NAME", "      WHERE r.OBJECT_TYPE")
    assert pre.count(registry_map) == 1 and pre.count(suffix_join) == 1 and pre.count(terminal_30) == 1
    assert "IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')" in pre
    for grid in ("-- P172.1 ", "-- P172.2 ", "-- P172.3 ", "-- P172.4 ", "-- P172.5 ", "-- P172.6 "):
        assert pre.count(grid) == 1, grid


def test_v172_part_b_fragments_are_v172_only(tmp_path):
    part_b = _extras(tmp_path)["part_b"]
    has = re.findall(r"'V172\.1 (\w+) DDL has: ([^']*)'", part_b)
    lacks = re.findall(r"'V172\.1 (\w+) DDL lacks: ([^']*)'", part_b)
    assert {p for p, _ in has} == set(_PROCS) == {p for p, _ in lacks}
    for proc, frag in has:
        assert frag in _PROCS[proc][0] and frag not in _PROCS[proc][1], (proc, frag)
    for proc, frag in lacks:
        assert frag not in _PROCS[proc][0] and frag in _PROCS[proc][1], (proc, frag)


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_ARM_RE = re.compile(r"SELECT '(AnomalySweep|ChangeImpactScan)', '(\w+)',[^;]*?'([^']*)', CURRENT_ROLE\(\)", re.S)


def test_v172_error_log_checks_flag_only_arms_new_since_the_apply(tmp_path):
    """Review r1 (V172.4 / R172.4). APPLIED_AT (the RUN_NEXT session, Central-pinned by correction 4) and LOGGED_AT
    (the 06:50 / 07:00 Central tasks) are both Central wall-clock, so the old -6h margin only reached back over the
    last OLD-body run; and every guarded arm that fails by design on this account (no TASK_VERSIONS, ORGANIZATION_USAGE
    or Cortex) read 'FAIL: n row(s)'. Now: the boundary is APPLIED_AT exactly, an arm is PAGE + ERROR_TYPE + CONTEXT,
    FAIL names only an arm silent in the 14 days before the apply, and PART B and R172.4 share that arm table."""
    ex = _extras(tmp_path)
    part_b, rep = ex["part_b"], ex["repair"]
    for sql in (part_b, rep):
        assert "'hour', -6" not in sql and "-6h" not in sql
    arms = _between(part_b, "    SELECT l.PAGE, l.ERROR_TYPE, l.CONTEXT,", "    HAVING COUNT_IF(l.LOGGED_AT >= a.T) > 0\n")
    assert part_b.count(arms) == 1 and rep.count(arms) == 1                    # one arm table, both grids
    assert "GROUP BY l.PAGE, l.ERROR_TYPE, l.CONTEXT" in arms
    assert "COUNT_IF(l.LOGGED_AT < a.T) AS ROWS_BEFORE" in arms
    assert "ON l.LOGGED_AT >= DATEADD('day', -14, a.T)" in arms
    assert "JOIN (SELECT MAX(APPLIED_AT) AS T FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172) a" in arms
    v4 = part_b[part_b.index("SELECT 'V172.4 "):]
    assert "WHEN MAX(a.T) IS NULL THEN 'FAIL: SCHEMA_VERSION has no 172 row'" in v4      # never OK unapplied
    assert "WHEN COUNT_IF(x.ROWS_BEFORE = 0) > 0" in v4 and "ELSE 'OK' END" in v4
    r4 = rep[rep.index("-- R172.4 "):]
    assert "IFF(x.ROWS_BEFORE = 0, 'NEW since V172', 'PRE-EXISTING') AS ARM_STATUS" in r4
    assert "EQUAL_NULL(l.CONTEXT, x.CONTEXT)" in r4
    # the CONTEXT literal is what tells two arms of one ERROR_TYPE apart (volume drop vs DQ_BREACH): every guarded
    # arm of the two logging procs is a distinct (PAGE, ERROR_TYPE, CONTEXT), and the page list is exactly theirs
    found = _ARM_RE.findall(_body(_CI) + _body(_SW))
    assert len(found) == 9 and len(set(found)) == 9, found
    assert len({(pg, et) for pg, et, _ in found}) == 8                          # dml_history_unavailable twice
    assert {pg for pg, _, _ in found} == {"AnomalySweep", "ChangeImpactScan"}
    for sql in (arms, v4):
        assert "('AnomalySweep', 'ChangeImpactScan')" in sql or "('AnomalySweep'), ('ChangeImpactScan')" in sql
    for proc in (_WH, _DR, _CS):                                                # the other three never log
        assert "APP_ERROR_LOG" not in proc


def test_v172_r172_1_reads_every_status_the_restamp_covers(tmp_path):
    """Review r1: R1b re-stamps OPEN / ACK / SNOOZED PERF_CHANGE_REGRESSION events, so the owner's list of alerts the
    re-computed verdict no longer supports must read the same set -- a snoozed false alert otherwise wakes back
    into triage (V086) still open. A SNOOZED one is woken first (the Alerts > Snoozed control) and then resolved."""
    rep = _extras(tmp_path)["repair"]
    r1 = _between(rep, "-- R172.1 ", "-- R172.2 ")
    statuses = re.search(r"AND e\.STATUS IN (\([^)]*\))", _repair("-- R1b live PERF")).group(1)
    assert "'SNOOZED'" in statuses and r1.count(f"e.STATUS IN {statuses}") == 1
    assert "Wake selected now" in r1 and 'st.button("Wake selected now"' in read("app/ui/pages/alerts.py")
    assert "never by SQL" in r1


def test_v172_r172_0_is_recommended_and_names_the_mixed_basis_gap(tmp_path):
    """Review r1: R4 re-freezes TASK baselines at apply while AFTER_* / VERDICT / VERDICT_DETAIL keep the last V140
    scan's attempt-based values, and R3 nulls PROCEDURE baselines under their old VERDICT -- the change table is on
    mixed bases until the next 06:50 scan. R172.0 (one scan, the daily task's work) closes the gap: recommended."""
    rep = _extras(tmp_path)["repair"]
    r0 = _between(rep, "-- R172.0 ", "-- R172.1 ")
    assert r0.startswith("-- R172.0 (RECOMMENDED, right after the apply")
    assert r0.count("-- CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN();") == 1          # still commented
    for phrase in ("mixed bases", "AFTER_CALLS", "VERDICT_DETAIL", "R3", "R4", "06:50"):
        assert phrase in r0, phrase
    head = rep[:rep.index("ALTER SESSION")]
    assert "R172.0" in head and "recommended" in head


# ---------------------------------------------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------------------------------------------
def test_v172_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert "not_ready EXCEPTION (-20172, 'V172 requires V171 first - apply migrations in order.');" in _MIG
    assert "IF (v < 171) THEN" in _MIG
    assert "SELECT 172 AS VERSION" in _MIG and "WHERE VERSION = 172);" in _MIG
    guard140 = _between(_V140, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard140.replace("-20140", "-20172").replace("'V140 requires V139 first", "'V172 requires V171 first")
            .replace("IF (v < 139)", "IF (v < 171)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 172);")
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "FIRST RUN:", "DELIVERY:", "ROLLBACK (order matters):",
                 "Apply AFTER V171. Idempotent; safe to re-run."):
        assert word in header, word


def _header() -> str:
    return _MIG[:_MIG.index("EXECUTE IMMEDIATE")]


def _flat(text: str) -> str:
    return " ".join(ln.lstrip("- ") for ln in text.splitlines())


def _rollback_null() -> str:
    """The baseline null the ROLLBACK note tells the owner to run, un-commented."""
    lines = _header()[_header().index("-- ROLLBACK"):].splitlines()
    i = next(k for k, ln in enumerate(lines) if ln[2:].lstrip().startswith("UPDATE "))
    pad = len(lines[i]) - len(lines[i][2:].lstrip()) - 2
    out = []
    for ln in lines[i:]:
        assert ln.startswith("--" + " " * pad), ln
        out.append(ln[2 + pad:])
        if ln.rstrip().endswith(";"):
            break
    return "\n".join(out)


def test_v172_rollback_nulls_the_baselines_v140_would_misread():
    """Holistic #13: R4 and the V172 scan freeze TASK baselines per scheduled run and PROCEDURE baselines by the
    anchored CALL match. V140's AFTER legs count every attempt and the bare suffix match, and V140 freezes only
    while BASELINE_FROM / BASELINE_CREDITS_PER_CALL IS NULL -- so baselines kept across a rollback are read on the
    other basis (a retried task: 14 runs / 0 failed before vs 21 / 7 after = REGRESSED, a false page). The note
    names the null to run right after V140's CREATE; the harness executes it against V140's own step 3."""
    head = _header()
    rb = head[head.index("-- ROLLBACK (order matters):"):head.index("-- Apply AFTER V171.")]
    assert "the re-frozen baselines stay" not in head
    stmt = _rollback_null()
    assert stmt == ("UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY\n"
                    "   SET BASELINE_FROM = NULL, BASELINE_CALLS = NULL, BASELINE_FAILS = NULL,\n"
                    "       BASELINE_MEDIAN_MS = NULL, BASELINE_P95_MS = NULL, BASELINE_CREDITS_PER_CALL = NULL\n"
                    " WHERE OBJECT_TYPE IN ('TASK', 'PROCEDURE') AND CURRENT_DATE() <= TRACKING_UNTIL AND NOT ALERTED;")
    assert set(re.findall(r"(\w+) = NULL", stmt)) == set(re.findall(r"(\w+) = NULL", _repair("-- R3 (R2-021)")))
    v140 = _body(_CI0)                                       # the claim the note rests on: V140 re-freezes NULLs only
    assert v140.count("r.BASELINE_FROM IS NULL\n") == 2 and v140.count("r.BASELINE_CREDITS_PER_CALL IS NULL") == 2
    flat = _flat(rb)
    for phrase in ("1. Re-run the base CREATEs", "The re-stamped COMPANY values stay",
                   "2. Right after V140's CREATE, before the next change-impact scan", "14 runs / 0 failed",
                   "21 / 7 after", "false PERF_CHANGE_REGRESSION", "false IMPROVED", "its own basis",
                   "a change older than 6 days gets a shorter baseline", "An ALERTED row keeps",
                   "raised between steps 1 and 2"):
        assert phrase in flat, phrase
    sqlglot = pytest.importorskip("sqlglot")
    parsed = sqlglot.parse(stmt, dialect="snowflake")
    assert len(parsed) == 1 and parsed[0] is not None and parsed[0].key == "update"


def test_v172_header_names_the_webhook_delivery_effect():
    """Holistic #12: "not re-raised" covers raising, not delivery. SP_NOTIFY_WEBHOOK (V164) picks events per route
    by COMPANY_FILTER (V034 set every route to 'ALFA') and keeps a per-(EVENT_ID, ROUTE_ID) ledger, so the five
    rules' UNKNOWN alerts stop reaching an ALFA-only route and a re-stamped live event can be delivered once to a
    newly matching route. The header says so and how to keep them posting."""
    head = _header()
    delivery = _flat(head[head.index("-- DELIVERY:"):head.index("-- ROLLBACK")])
    for phrase in ("SP_NOTIFY_WEBHOOK (V164)", "COMPANY_FILTER", "V034 set every existing route to 'ALFA'",
                   "once per (EVENT_ID, ROUTE_ID) in ALERT_DELIVERIES", "PERF_CHANGE_REGRESSION, PIPE_DT_FAILURES,",
                   "PIPE_VOLUME_DROP, DQ_BREACH and DQ_SCHEMA_DRIFT", "now UNKNOWN and stop posting to an ALFA-only route",
                   "no undelivered_expired row", "Unmapped entities", "add an ALL or UNKNOWN route",
                   "R1b / R2", "TASK_ALERT_NOTIFY", "24h; 7d for CRITICAL",
                   # an older event is not logged ONCE: V164's watchdog re-logs the pair every 24h until it is 7d old
                   "An older one raised within 7 days is not sent there: V164's watchdog logs an undelivered_expired "
                   "row for that route instead, then another every 24h (it skips a pair logged in the last 24h) while "
                   "the event stays OPEN and undelivered there, until it is 7 days old."):
        assert phrase in delivery, phrase
    assert "gets one undelivered_expired row" not in delivery
    v164 = read("snowflake/migrations/V164__notify_actionable_lines_escalation.sql")       # the facts it cites
    assert "AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')" in v164
    assert "WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id" in v164
    watchdog = v164[v164.index("    SELECT 'NotifyWebhook', 'undelivered_expired',"):v164.index("    expired := SQLROWCOUNT;")]
    for fact in ("    WHERE e.STATUS = 'OPEN'\n",
                 "      AND e.RAISED_AT < DATEADD('hour', -24, CURRENT_TIMESTAMP())\n",
                 "      AND e.RAISED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())\n",
                 "                      WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r2.ROUTE_ID)\n",
                 "                        AND a.LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()));\n"):
        assert watchdog.count(fact) == 1, fact
    dep = " ".join(read("DEPLOYMENT.md").replace("\n>", "\n").split())       # the apply note carries the same fact
    assert "logs one undelivered_expired row instead" not in dep
    assert ("V164's watchdog logs an undelivered_expired row for that route instead, then another every 24 h (it "
            "skips a pair logged in the last 24 h) while the event stays OPEN and undelivered there, until it is 7 "
            "days old.") in dep
    assert "   SET COMPANY_FILTER = 'ALFA'\n" in read("snowflake/migrations/V034__route_company_filter.sql")


def test_v172_file_order_and_statements():
    from tests.test_migrations_parse import _plain_statements
    order = [_MIG.index("EXCEPTION (-20172")]
    for name in _MARKERS:
        mark = _MIG.index(_MARKERS[name])
        create = _MIG.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}(")
        assert _MIG[mark:create].count("\n") == 1, name                   # the marker sits directly above
        order += [mark, create]
    order += [_MIG.index("-- R1 (R2-023)"), _MIG.index("-- R1b live PERF"), _MIG.index("-- R2 (R2-024)"),
              _MIG.index("-- R4 (R2-025)"), _MIG.index("-- R3 (R2-021)"),           # R3 LAST (holistic #15)
              _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")]
    assert order == sorted(order)
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 2)[:2] for s in _plain_statements(_MIG)]
    assert kinds == [["UPDATE", "DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY"],
                     ["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"],
                     ["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"],
                     ["UPDATE", "DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY"],
                     ["UPDATE", "DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY"],
                     ["INSERT", "INTO"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 5 and _MIG.count("$$") == 12
    assert "$$" not in _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG) and "\r" not in _MIG and "\\" not in _MIG


def test_v172_nothing_runs_at_apply_and_no_new_object():
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "DELETE", "MERGE", "VIEW",
                   "FUNCTION", "TABLE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert len(re.findall(r"\bCREATE\b", top)) == 5                                 # the five procs only
    assert "SOURCE_FRESHNESS_STATE" not in _MIG                           # test_freshness_coverage's lock


def test_v172_in_expected_migrations():
    """Integrator lockstep: Admin lists V172 with house-rule text (no $, no hand CALL, no trailing '.'). admin.py
    is integration-only, so this is red in the cluster branch until the integrator adds the entry."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[172])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    for phrase in ("COMPANY_FOR_DATABASE", "Hr/Min/Sec", "scheduled run", "No task change, no apply-time run"):
        assert phrase in text, phrase


def test_v172_description_fits_and_has_no_apostrophe():
    desc = re.search(r"SELECT 172 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and "$" not in desc and len(desc) <= 4000
    for item in ("R2-023", "R2-021", "R2-025", "R2-022", "R1-124", "R2-024", "R2-095", "R1-227", "CORTEX-NULLIF"):
        assert item in desc, item


# ---------------------------------------------------------------------------------------------------------------
# Lineage + round 13
# ---------------------------------------------------------------------------------------------------------------
def _assert_v172_lineage(texts: dict[int, str]) -> None:
    """Bounded at 172 (the test_v165 W16 lesson): a later re-derivation of any of the five must not turn it red."""
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _violations
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    defs = _definers(texts)
    for name, (_new, _old, base) in _PROCS.items():
        r = rows[(172, name)]
        assert r["src"] == "marker" and r["claims"] == [base] and r["prev"] == base and not r["waived"], r
        assert [v for v in defs[name] if base <= v <= 172] == [base, 172], name
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V172 ")]


def test_v172_markers_name_the_current_definers():
    from tests.test_proc_lineage import _migrations
    _assert_v172_lineage(_migrations())
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 5


def test_v172_lineage_lock_survives_a_later_re_derivation():
    from tests.test_proc_lineage import _migrations
    texts = _migrations()
    assert max(texts) < 999
    texts[999] = "".join(f"-- >>> derived:{n}  (from V172)\nCREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{n}()\n"
                         "RETURNS VARCHAR LANGUAGE SQL AS\n$$\nBEGIN\n    RETURN 'x';\nEND;\n$$;\n" for n in _PROCS)
    _assert_v172_lineage(texts)


@pytest.mark.parametrize("name", list(_PROCS))
def test_v172_proc_normalizes_back_to_its_base_byte_for_byte(name):
    new, old, _base = _PROCS[name]
    assert new != old
    assert _REVERSE[name](new) == old


@pytest.mark.parametrize(("name", "victim"), [
    ("SP_CHANGE_IMPACT_SCAN", "AND PROCEDURE_CATALOG <> 'DBA_MAINT_DB'"),
    ("SP_CHANGE_IMPACT_SCAN", "'attribution_unavailable'"),
    ("SP_CHANGE_IMPACT_SCAN", "SET BASELINE_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.BASELINE_CALLS, 0)"),
    ("SP_WAREHOUSE_CHANGE_SCAN", "COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS')"),
    ("SP_SCAN_SCHEMA_DRIFT", "WHERE SNAPSHOT_DAY < DATEADD('day', -90, CURRENT_DATE())"),
    ("SP_SCAN_CLOUD_SVC_ANOMALY", "AND l.ACTIVE_DAYS >= 10"),
    ("SP_ANOMALY_SWEEP", "RETURN 'anomaly sweep v3 complete'"),
    ("SP_ANOMALY_SWEEP", "'cortex_pre_explain_unavailable'"),
])
def test_v172_normalize_has_teeth(name, victim):
    """One character deleted anywhere outside the declared deltas breaks the byte compare."""
    new, old, _base = _PROCS[name]
    assert new.count(victim) >= 1
    assert _REVERSE[name](new.replace(victim, victim[:-1], 1)) != old


# ---------------------------------------------------------------------------------------------------------------
# R2-023 / R2-024: COMPANY by the V044 rule
# ---------------------------------------------------------------------------------------------------------------
def test_v172_no_raw_company_guess_in_any_re_derived_body():
    for name, (new, _old, _base) in _PROCS.items():
        assert not _GUESS.search(new), name
    assert len(_GUESS.findall(_CI0)) == 2 and len(_GUESS.findall(_SW0)) == 3 and len(_GUESS.findall(_DR0)) == 1


def test_v172_change_impact_company_is_the_v030_shape():
    assert _CI.count("COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY") == 1
    assert _CI.count("COMPANY_FOR_DATABASE(DATABASE_NAME) AS COMPANY") == 1
    assert "        GROUP BY 1, 2, 3, 4\n        ) g\n    ) s\n" in _CI and "GROUP BY 1, 2, 3, 4, 5" not in _CI
    assert not re.search(r"COMPANY_FOR_\w+\(\s*(?:MAX|MIN|ANY_VALUE|SUM|COUNT)\(", _MIG)
    # the MERGE key (OBJECT_TYPE, OBJECT_NAME, CHANGE_SEEN_AT) carries no COMPANY, so no row splits; step 7 copies it
    assert "    ON t.OBJECT_TYPE = s.OBJECT_TYPE AND t.OBJECT_NAME = s.OBJECT_NAME\n" in _CI
    assert "    SELECT c.RULE_ID, r.COMPANY,\n" in _CI


@pytest.mark.parametrize(("proc", "arg", "key"), [
    ("SP_SCAN_SCHEMA_DRIFT", "SPLIT_PART(a.FQN, '.', 1)", _KEY_DRIFT),
    ("SP_ANOMALY_SWEEP", "d.DATABASE_NAME", _KEY_DT),
    ("SP_ANOMALY_SWEEP", "v.DB", _KEY_VOL),
    ("SP_ANOMALY_SWEEP", "s.DB", _KEY_DQ),
])
def test_v172_wrapped_arm_keeps_its_dedupe_key(proc, arg, key):
    """The b (...) wrapper only moves the NOT EXISTS outside: the arm's 7th select item IS the old NOT EXISTS key, so
    what dedupes is unchanged."""
    body, base = _PROCS[proc][0], _PROCS[proc][1]
    udf = f"DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE({arg}),"
    assert body.count(udf) == 1
    inner = body[body.index(udf):]
    inner = inner[:inner.index(f") b {_B_COLS}")]
    assert _norm(inner).count(_norm(key)) == 1           # the 7th column of the inner SELECT ...
    assert f"WHERE e.DEDUPE_KEY = {key}" in base          # ... is what V133 / V150 deduped on


def test_v172_sweep_keeps_every_arm():
    for needle in ("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT();",
                   "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();", "-- DQ_BREACH (R24)",
                   "'PIPE_VOLUME_DROP'", "'PIPE_DT_FAILURES'", "'COST_ORG_ACCOUNT_CREEP'",
                   "'PERF_FINGERPRINT_DRIFT'", "RETURN 'anomaly sweep v3 complete';"):
        assert _SW.count(needle) == _SW0.count(needle) >= 1, needle
    assert _SW.count("WHERE e.DEDUPE_KEY = b.DEDUPE_KEY") == 3 and _SW.count("RETURN ") == _SW0.count("RETURN ") == 1


# ---------------------------------------------------------------------------------------------------------------
# R2-021 anchored match; R2-025 terminal attempt; R2-022 settled per-run credits/call
# ---------------------------------------------------------------------------------------------------------------
def test_v172_anchored_call_match_counts():
    assert _CI.count("REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')") == 0
    assert _CI0.count("REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')") == 4
    assert _CI.count("POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '('") == 4
    assert _CI.count("POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '('") == 4
    assert _CI.count("REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')") == 8
    assert _CI.count("AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'") == 4   # pre-filter
    # the step-2 DDL-evidence match is deliberately untouched (evidence only)
    assert "AND POSITION(SPLIT_PART(t.OBJECT_NAME, '.', 3) IN UPPER(d.QUERY_TEXT)) > 0;" in _CI


def test_v172_task_count_legs_collapse_and_credit_legs_do_not():
    pre, post = _CI.split("-- 5) Measured credits/call", 1)
    q = "QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME"
    assert pre.count(q) == 2 and post.count(q) == 0
    assert pre.count("ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1") == 2
    assert pre.count("JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h") == 0
    assert post.count("JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h") == 2            # every attempt's credits
    assert pre.count("WHERE SCHEDULED_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())") == 2
    # STATE filtered BEFORE the window, like the drill
    leg = _between(pre, "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY", ") h\n")
    assert leg.index("AND STATE IN ('SUCCEEDED', 'FAILED')") < leg.index("QUALIFY")
    assert _CI.count("QUERY_TYPE = 'CALL'") == _CI0.count("QUERY_TYPE = 'CALL'") == 4
    assert pre.count(_ON_BASE) == 1 and pre.count(_ON_AFTER) == 1                    # ON predicates unchanged


def test_v172_after_credits_per_call_is_settled_per_run():
    after = _between(_CI, "           SET AFTER_CREDITS_PER_CALL = s.CR_PER_CALL\n", "    EXCEPTION\n")
    base = _between(_CI, "           SET BASELINE_CREDITS_PER_CALL", "        UPDATE DBA_MAINT_DB.OVERWATCH")
    assert "NULLIF(t.AFTER_CALLS, 0)" not in _CI
    assert "SET BASELINE_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.BASELINE_CALLS, 0)" in _CI   # baseline untouched
    assert _rev(base, _pred_new(" " * 19), _pred_old(" " * 19)) == _between(  # only R2-021's anchoring moved
        _CI0, "           SET BASELINE_CREDITS_PER_CALL", "        UPDATE DBA_MAINT_DB.OVERWATCH")
    assert "SUM(COALESCE(a.CR, 0)) / NULLIF(COUNT(DISTINCT x.RUN_KEY), 0) AS CR_PER_CALL" in after
    assert "HAVING COUNT(a.RID) > 0" in after and "              LEFT JOIN (\n" in after
    assert "q.QUERY_ID AS RUN_KEY" in after and "r.OBJECT_NAME || '|' || TO_VARCHAR(h.SCHEDULED_TIME) AS RUN_KEY" in after
    assert _CI.count(_SETTLED) == 4 == _CI0.count(_SETTLED) + 2                      # 2 baseline + 2 new
    assert after.count(f"q.START_TIME < {_SETTLED}") == 1 and after.count(f"h.QUERY_START_TIME < {_SETTLED}") == 1
    assert _CI.count(f"q.START_TIME < {_SETTLED}") == 1                              # step-4 AFTER_* unbounded
    assert _CI.count("LEFT JOIN (") == _CI0.count("LEFT JOIN (") + 1


# ---------------------------------------------------------------------------------------------------------------
# R1-124: Hr/Min/Sec
# ---------------------------------------------------------------------------------------------------------------
def _mirror(s: Decimal | None, nul: str) -> str:
    """Python twin of the HD CASE (Decimal arithmetic, HALF_TO_EVEN), to compare with humanize_duration."""
    if s is None:
        return nul
    if (s * 1000).quantize(Decimal(1), ROUND_HALF_EVEN) == 0:
        return "0s"
    if s < 1:
        return f"{int((s * 1000).quantize(Decimal(1), ROUND_HALF_EVEN))}ms"
    if s < 10:
        return f"{s.quantize(Decimal('0.1'), ROUND_HALF_EVEN)}s"
    r = int(s.quantize(Decimal(1), ROUND_HALF_EVEN))
    out = f"{r // 3600}h " if r >= 3600 else ""
    out += f"{(r // 60) % 60}m " if (r // 60) % 60 > 0 else ""
    out += f"{r % 60}s" if r < 3600 and r % 60 > 0 else ""
    return out.strip()


def test_v172_hd_template_matches_humanize_duration():
    from app.logic.formulas import humanize_duration
    grid = [Decimal(x) for x in ("0", "0.0004", "0.5", "0.9996", "1", "5.04", "9.96", "10", "45", "59.4", "60",
                                 "94.5", "95.5", "119.6", "1800", "2400", "3599.6", "3600", "3630", "3660", "8700",
                                 "12000", "86400", "90061")]
    grid += [Decimal(n) / 100 for n in range(0, 2_000_000, 997)]                     # NUMBER(18,2) operands
    checked = 0
    for s in grid:
        if Decimal(1) <= s < 10 and (s * 100) % 10 == 5:
            continue                                # an exact x.x5 tie: Python formats the binary float
        assert _mirror(s, "?") == humanize_duration(float(s), "s"), s
        checked += 1
    assert checked > 1900 and _mirror(None, "?") == "?"


def test_v172_verdict_details_use_the_template():
    assert _CI.count(_hd("(BASELINE_P95_MS / 1000)", "?", _C)) == 1
    assert _CI.count(_hd("(AFTER_P95_MS / 1000)", "?", _C)) == 1
    assert _WH.count(_hd("(BASELINE_P95_S)", "?", _C)) == 1 and _WH.count(_hd("(AFTER_P95_S)", "?", _C)) == 1
    assert _WH.count(_hd("(COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60)", "0s", _C)) == 1
    assert _WH.count(_hd("(COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60)", "0s", _C)) == 1
    assert _CI.count("|| ' -> '\n") == 1 and _WH.count("|| ' -> '\n") == 2 and "|| '/day'\n" in _WH
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    latest = _latest_proc_bodies()
    for name in ("SP_CHANGE_IMPACT_SCAN", "SP_WAREHOUSE_CHANGE_SCAN"):
        assert "|| 's->'" not in latest[name] and "' min/d'" not in latest[name]
        assert "ROUND(BASELINE_P95_MS / 1000, 1)::VARCHAR" not in latest[name]
        assert "::FLOAT" not in latest[name] and "TO_DOUBLE" not in latest[name]    # HALF_TO_EVEN needs fixed-point


def test_v172_humanized_detail_fits_and_the_app_shim_leaves_it_alone():
    from app.logic.wh_change import humanize_verdict_detail
    wh = (f"credits/day 99999999.99->99999999.99 | p95 {_mirror(Decimal('99999999999.9'), '?')} -> "
          f"{_mirror(Decimal('99999999999.9'), '?')} | queue {_mirror(Decimal('9999999999.99') * 60, '0s')} -> "
          f"{_mirror(Decimal('9999999999.99') * 60, '0s')}/day | fail 100.00->100.00% | 999999999999->999999999999 "
          "queries")
    obj = (f"runs 999999999999->999999999999 | fails 999999999999->999999999999 | p95 "
           f"{_mirror(Decimal('9999999999999999.99') / 1000, '?')} -> {_mirror(Decimal('0.5'), '?')} | credits/call "
           "999999999999.1234->999999999999.1234")
    assert len(wh) <= 500 and len(obj) <= 500                                      # VERDICT_DETAIL VARCHAR(500)
    for text in (wh, obj, "credits/day 10.5->12.25 | p95 5.0s -> 6.1s | queue 0s -> 30s/day"):
        assert humanize_verdict_detail(text) == text


def test_v172_scans_and_the_app_shim_write_the_same_arrow():
    """Review r1: the 90-day drills (Operations object / warehouse change, Workbench Recent changes, rca Magnitude)
    list closed pre-V172 rows -- re-rendered by the shim -- beside V172 rows humanized in SQL. Both must print the
    scans' ASCII ' -> ' (every line V172 adds is ASCII), so an old row reads exactly like a new one."""
    from app.logic.wh_change import humanize_verdict_detail
    sql_arrows = re.findall(r"^\s*\|\| '([^']*->[^']*)'$", _body(_CI) + _body(_WH), re.M)
    assert sql_arrows == [" -> "] * 3                                 # between each pair of HD CASE blocks
    old_wh = "credits/day 1.0->1.0 | p95 1800.0s->2400.0s | queue 145.00->200.00 min/d | fail 0->0% | 20->20 queries"
    new_wh = (f"credits/day 1.0->1.0 | p95 {_mirror(Decimal('1800.0'), '?')}{sql_arrows[0]}"
              f"{_mirror(Decimal('2400.0'), '?')} | queue {_mirror(Decimal('145.00') * 60, '0s')}{sql_arrows[0]}"
              f"{_mirror(Decimal('200.00') * 60, '0s')}/day | fail 0->0% | 20->20 queries")
    assert humanize_verdict_detail(old_wh) == new_wh == ("credits/day 1.0->1.0 | p95 30m -> 40m "
                                                         "| queue 2h 25m -> 3h 20m/day | fail 0->0% | 20->20 queries")
    old_obj = "runs 10->12 | fails 0->1 | p95 ?s->95.5s | credits/call n/a->n/a"
    assert humanize_verdict_detail(old_obj) == (f"runs 10->12 | fails 0->1 | p95 ?{sql_arrows[0]}"
                                                f"{_mirror(Decimal('95.5'), '?')} | credits/call n/a->n/a")
    assert "\u2192" not in read("app/logic/wh_change.py")


# ---------------------------------------------------------------------------------------------------------------
# R2-095 / R1-227 / CORTEX-NULLIF
# ---------------------------------------------------------------------------------------------------------------
def test_v172_org_creep_pointer_is_true_and_fits_the_push_excerpt():
    from app.logic.navigate import investigation_target
    assert "Admin > Org spend" not in _SW and _SW.count(_PTR_NEW) == 1
    page, section = re.search(r"Breakdown: ([^>]+?) > ([^.']+)\.", _PTR_NEW).groups()
    target = investigation_target("COST_ORG_ACCOUNT_CREEP", "ACME org spend up 250% week-over-week")
    assert (page, section) == (target["page"], target["section"])               # the drawer's Open goes there too
    assert f'"{section}"' in read("app/ui/pages/cost.py") and f'"{page}": cost.render' in read("app/main.py")
    assert len("Last 7d 99999999 vs prior 99999999 USD" + "." + _PTR_NEW.split("'.")[1].split("'")[0]) <= 100
    assert ("c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))"
            in _SW)                                                               # the key: no re-fire


def test_v172_cloud_svc_scan_gates_on_an_enabled_count():
    assert "IF (:zthr IS NULL)" not in _CS
    assert _CS.index("IF (:enabled_cnt = 0) THEN") < _CS.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")
    assert "RETURN 'cloud-services anomaly scan skipped (rule disabled)';" in _CS
    assert "COALESCE(MAX(THRESHOLD_NUM), 3.5)" in _CS                               # a NULL threshold keeps 3.5
    assert "WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY' AND ENABLED;" in _CS
    assert re.search(r"^    enabled_cnt INT;$", _body(_CS), re.M)


def test_v172_cost_anomaly_sweep_rider_is_a_predicate_not_a_return():
    ins = _between(_SW, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS", "    -- Dynamic-table refresh failures")
    assert ins.count("AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n"
                     "                  WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED)") == 1
    assert ins.index("AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG") < ins.index("AND NOT EXISTS (")
    assert "RETURN" not in _strip_noise(ins)


def test_v172_cortex_model_read_matches_the_app():
    import app.core.ai as ai
    from app.config import DEFAULT_SETTINGS
    assert _SW.count(_CORTEX_NEW) == 1 and "COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL'" not in _SW
    lit = re.search(r"RLIKE\(cm, '([^']+)'\), cm, '([^']+)'\)", _SW)
    assert lit and "\\" not in lit.group(1)
    assert "^" + lit.group(1).replace(".-]", r".\-]") + "$" == ai._MODEL_RE.pattern
    assert lit.group(2) == ai._DEFAULT_MODEL == DEFAULT_SETTINGS["CORTEX_MODEL"]

    def sql_twin(v: str | None) -> str:            # LOWER(TRIM(v)) (TRIM strips spaces) + whole-string RLIKE
        cm = None if v is None else v.strip(" ").lower()
        return cm if cm is not None and re.fullmatch(lit.group(1), cm) else lit.group(2)

    for v in (None, "", "   ", " Llama3.1-70B ", "llama3.1 8b", "llama3_1", "mistral-large2", "claude-sonnet-4-5",
              "a" * 62, "x"):
        assert sql_twin(v) == ai.normalize_model(v), v


# ---------------------------------------------------------------------------------------------------------------
# The repairs
# ---------------------------------------------------------------------------------------------------------------
def _repair(tag: str) -> str:
    i = _MIG.index(tag)
    return _MIG[i:_MIG.index(";\n", i) + 2]


def test_v172_company_restamps_split_the_database_out_of_the_fqn():
    """Correction 4: DEDUPE_KEY part 2 is an object FQN for all five rules; the UDF gets part 2 up to its first dot,
    never part 2 whole (that would re-stamp every COMPANY_SCOPE-mapped database UNKNOWN)."""
    fqn = "SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB"
    for tag in ("-- R1b live PERF", "-- R2 (R2-024)"):
        r = _repair(tag)
        assert r.count(fqn) == 1 and "COMPANY_FOR_DATABASE(x.DB)" in r, tag
        assert "SPLIT_PART(e.DEDUPE_KEY, '|', 2))" not in r and "COMPANY_FOR_DATABASE(SPLIT_PART" not in r
        assert "AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')" in r
        assert ("AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m\n"
                "                                WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)") in r
        assert "AND t.COMPANY IS DISTINCT FROM s.NEW_COMPANY;" in r
    r1b = _repair("-- R1b live PERF")
    assert "WHERE e.RULE_ID = 'PERF_CHANGE_REGRESSION'" in r1b
    assert "COALESCE(g.COMPANY, k.FQN_COMPANY) AS NEW_COMPANY" in r1b              # the registry row first
    assert ("'PERF_CHANGE_REGRESSION|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE) AS DEDUPE_KEY"
            in r1b)
    assert ("WHERE e.RULE_ID IN ('DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP', 'DQ_BREACH')"
            in _repair("-- R2 (R2-024)"))
    r1 = _repair("-- R1 (R2-023)")
    assert "FROM (SELECT DISTINCT DATABASE_NAME FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY) d" in r1
    assert "AND t.COMPANY IS DISTINCT FROM m.CO;" in r1


def test_v172_baseline_null_is_first_apply_procedure_only():
    r3 = _repair("-- R3 (R2-021)")
    assert "NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172)" in r3
    assert "WHERE r.OBJECT_TYPE = 'PROCEDURE'" in r3 and "CURRENT_DATE() <= r.TRACKING_UNTIL" in r3
    assert "ON ENDSWITH(UPPER(p.PROCEDURE_NAME), UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3)))" in r3
    assert "AND UPPER(p.PROCEDURE_NAME) <> UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3))" in r3      # strict suffix
    assert "DELETED" not in r3.split("UPDATE", 1)[1]                              # deleted procs included
    sets = set(re.findall(r"(\w+) = NULL", r3))
    assert sets == {"BASELINE_FROM", "BASELINE_CALLS", "BASELINE_FAILS", "BASELINE_MEDIAN_MS", "BASELINE_P95_MS",
                    "BASELINE_CREDITS_PER_CALL"}
    assert "ALERT_EVENTS" not in r3
    # Holistic #15: the first-apply gate is "no 172 row", so nothing that can stop the file may sit between R3's
    # commit and that row. R3 is the LAST repair: the version row is the very next statement (the harness executes
    # every stop point: test_r3_runs_last_so_a_retry_never_re_nulls_a_baseline_a_scan_re_froze).
    assert _MIG[_MIG.index(r3) + len(r3):].startswith("\nINSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert "runs LAST" in r3 and "R4's 30-day TASK_HISTORY read" in r3


def test_v172_task_refreeze_is_terminal_attempt_and_rescaled():
    r4 = _repair("-- R4 (R2-025)")
    assert "r.OBJECT_TYPE = 'TASK' AND r.BASELINE_FROM IS NOT NULL AND r.BASELINE_CALLS > 0" in r4
    assert "CURRENT_DATE() <= r.TRACKING_UNTIL" in r4
    assert "BASELINE_CREDITS_PER_CALL = s.OLD_CPC * s.OLD_CALLS / NULLIF(s.CALLS, 0)" in r4
    assert "WHERE SCHEDULED_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())" in r4
    assert "ON h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)" in r4
    assert "-20, CURRENT_TIMESTAMP" not in r4                                     # no now-20d clip
    assert r4.index("AND STATE IN ('SUCCEEDED', 'FAILED')") < r4.index("QUALIFY ROW_NUMBER()")
    assert "BASELINE_FROM =" not in r4                                            # the freeze date stays


def test_v172_plain_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(_MIG))
    assert len(stmts) == 6
    for s in stmts:
        parsed = sqlglot.parse(s, dialect="snowflake")
        assert len(parsed) == 1 and parsed[0] is not None, s[:80]


_BINDS = {"trk_lo": "CURRENT_TIMESTAMP()", "pct": "50", "min_calls": "5", "zthr": "3.5", "cs_floor": "1.0",
          "credit_price": "3.68"}


def _bind(sql: str) -> str:
    """:name binds -> literals, outside string literals only ('[[:space:]]' is a pattern, not a bind)."""
    parts = re.split(r"('(?:[^']|'')*')", sql)
    for k in range(0, len(parts), 2):
        parts[k] = re.sub(r"(?<![:\w]):([a-z_]+)\b", lambda m: _BINDS[m.group(1)], parts[k])
    return "".join(parts)


_CHANGED = [   # (proc, anchor, occurrence): the statement that holds the n-th anchor
    ("SP_CHANGE_IMPACT_SCAN", "SELECT g.OBJECT_TYPE, g.DATABASE_NAME", 0),            # arm 1a (R2-023)
    ("SP_CHANGE_IMPACT_SCAN", "COMPANY_FOR_DATABASE(DATABASE_NAME) AS COMPANY", 0),   # arm 1b (R2-023)
    ("SP_CHANGE_IMPACT_SCAN", "POSITION('CALL' || SPLIT_PART", 0),                    # step 3 PROCEDURE (R2-021)
    ("SP_CHANGE_IMPACT_SCAN", "QUALIFY ROW_NUMBER()", 0),                             # step 3 TASK (R2-025)
    ("SP_CHANGE_IMPACT_SCAN", "POSITION('CALL' || SPLIT_PART", 1),                    # step 4 PROCEDURE
    ("SP_CHANGE_IMPACT_SCAN", "QUALIFY ROW_NUMBER()", 1),                             # step 4 TASK
    ("SP_CHANGE_IMPACT_SCAN", "POSITION('CALL' || SPLIT_PART", 2),                    # step 5 baseline credits
    ("SP_CHANGE_IMPACT_SCAN", "AS CR_PER_CALL", 0),                                   # step 5 after (R2-022)
    ("SP_CHANGE_IMPACT_SCAN", "HALF_TO_EVEN", 0),                                     # step 6 (R1-124)
    ("SP_WAREHOUSE_CHANGE_SCAN", "HALF_TO_EVEN", 0),
    ("SP_SCAN_SCHEMA_DRIFT", "COMPANY_FOR_DATABASE(SPLIT_PART", 0),
    ("SP_SCAN_CLOUD_SVC_ANOMALY", "INTO :enabled_cnt, :zthr", 0),
    ("SP_ANOMALY_SWEEP", "WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED)", 0),
    ("SP_ANOMALY_SWEEP", "COMPANY_FOR_DATABASE(d.DATABASE_NAME)", 0),
    ("SP_ANOMALY_SWEEP", "COMPANY_FOR_DATABASE(v.DB)", 0),
    ("SP_ANOMALY_SWEEP", "COMPANY_FOR_DATABASE(s.DB)", 0),
    ("SP_ANOMALY_SWEEP", "Cost Intelligence > Contract & Forecast", 0),
    ("SP_ANOMALY_SWEEP", "RLIKE(cm, ", 0),
]


def _drop_comments(sql: str) -> str:
    """-- comments out, single-quoted literals ('' escapes) KEPT."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _statement(body: str, anchor: str, occurrence: int) -> str:
    """The SQL statement that holds the n-th anchor: from just after the previous ';' line end to the next one,
    comments and leading scripting lines (BEGIN) dropped, a scripting INTO :x removed, :binds made literals."""
    pos = -1
    for _ in range(occurrence + 1):
        pos = body.index(anchor, pos + 1)
    chunk = _drop_comments(body[body.rfind(";\n", 0, pos) + 2:body.index(";\n", pos) + 1])
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    while lines and not re.match(r"\s*(?:MERGE|UPDATE|INSERT|SELECT|WITH)\b", lines[0]):
        lines.pop(0)
    stmt = re.sub(r"\s+INTO :\w+(?:, :\w+)*", "", "\n".join(lines))
    return _bind(stmt)


@pytest.mark.parametrize(("proc", "anchor", "occurrence"), _CHANGED)
def test_v172_changed_proc_statements_parse(proc, anchor, occurrence):
    """Every re-derived statement V172 touches parses as Snowflake SQL with its :binds as literals (sqlglot cannot
    read the scripting wrapper, so each statement is cut out of the body)."""
    sqlglot = pytest.importorskip("sqlglot")
    stmt = _statement(_body(_PROCS[proc][0]), anchor, occurrence)
    assert re.match(r"(?:MERGE|UPDATE|INSERT|SELECT)\b", stmt.lstrip()), stmt[:80]
    assert anchor.replace(" INTO :enabled_cnt, :zthr", "COUNT(*), COALESCE") in stmt or "INTO :" in anchor
    parsed = sqlglot.parse(stmt, dialect="snowflake")
    assert len(parsed) == 1 and parsed[0] is not None, stmt[:160]


def test_v172_parse_check_has_teeth():
    sqlglot = pytest.importorskip("sqlglot")
    stmt = _repair("-- R4 (R2-025)")
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse(stmt.replace("JOIN (SELECT", "JOIN ((SELECT", 1), dialect="snowflake")
    with pytest.raises(KeyError):
        _bind("SELECT :not_a_bind")
    stmt = _statement(_body(_CI), "AS CR_PER_CALL", 0)
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse(stmt.replace("LEFT JOIN (", "LEFT JOIN ((", 1), dialect="snowflake")
