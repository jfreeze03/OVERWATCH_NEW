"""Locks for V169 -- SP_ALERT_SCAN_DAILY windows and keys (round-2 review, alerts cluster: R2-041, R2-042, R2-103,
R2-044, R2-020 = R2-043, R2-047, R1-071, R1-233), one re-derivation from V163 (its current definer).

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v169_harness.py EXECUTES the arms. What this file proves:
  * generation -- outputs/gen_v169.py regenerates the migration byte-for-byte and reads only its base; the
    read-only PREFLIGHT / PART B and the comment-only repair notes are written only on request and parse;
  * shape -- first line, guard (-20169, v < 168), the marker + SP_ALERT_SCAN_DAILY, the guarded NAME refresh, the
    version row; nothing runs at apply time;
  * lineage + round 13 -- reversing every declared delta (the test's OWN copies) gives V163's body back
    byte-for-byte, a stray edit elsewhere breaks the compare, and every delta is real;
  * the deltas -- per-arm locks, the untouched arms ([25] for test_v160, the DAILY_BURN text for test_rec10), the
    tally, the footprint, the R2-045 needles in tests/test_r2_alerts_logic.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._source import ROOT, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V169__alert_scan_daily_windows_and_keys.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V163 = read("snowflake/migrations/V163__ai_runaway_trust_regression.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _body(proc: str) -> str:
    return proc[proc.index("$$") + 2:proc.rindex("$$")]


_D = _proc(_MIG, "SP_ALERT_SCAN_DAILY()")
_D163 = _proc(_V163, "SP_ALERT_SCAN_DAILY()")

# ---------------------------------------------------------------------------------------------------
# Test-side copies of every declared delta (independent of outputs/gen_v169.py).
# ---------------------------------------------------------------------------------------------------
_MARKER = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V163; [08]/[09] complete-day MTD, [16] contract start gate + "
           "end bound, [12] live DATABASE_ID, [18] error-cycle-day key, [19] previous-day true egress, [24] NULL "
           "timer as 0, [29] threshold floor, V169)\n")
_S08 = ("    -- [08] COST_BUDGET_PACE\n", "    -- [09] COST_FORECAST_BREACH\n")
_S09 = ("    -- [09] COST_FORECAST_BREACH\n", "    -- [13b] COST_AI_CREEP\n")
_S16 = ("    -- [16] COST_CONTRACT_BREACH\n", "    -- [12] COST_STORAGE_SURGE\n")
_S12 = ("    -- [12] COST_STORAGE_SURGE\n", "    -- [13] COST_SERVERLESS_CREEP\n")
_S19 = ("    -- [19] COST_EGRESS_SPIKE", "    -- [22] OPS_PIPELINE_DEGRADED")
_S24 = ("    -- [24] COST_IDLE_OPPORTUNITY", "    -- [25] COST_SLEEP_POLLING")
_S29 = ("    -- [29] SEC_TRUST_REGRESSION", "    -- [17] PIPE_REF_GAP")
_S18 = ("    -- [18] DQ_RECON_ERROR", "    IF (fails > 0) THEN")
_AI = ("(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR "
       "SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%')")
_MTD_ADD = (
    "            -- V169 (R2-041): month-to-date over COMPLETE days only (DAY < today), the numerator of\n"
    "            -- DAILY_RATE_USD. MTD_USD also holds the partial UTC row of today (the ~06:45 load sees part of\n"
    "            -- it), which the completed-days pace allowance ((DAY_OF_MONTH - 1) / DAYS_IN_MONTH) never budgets.\n"
    f"            SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT {_AI} THEN CREDITS_BILLED ELSE 0 END) * :credit_price\n"
    f"              + SUM(CASE WHEN DAY < CURRENT_DATE() AND {_AI} THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price "
    "AS MTD_COMPLETE_USD,\n")
_XFER = """        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        xfer AS (
            -- V169 (R2-047): one row per Central day and destination. TRUE egress only -- the predicate of the
            -- Security > Egress drill (security_sql.egress_baseline): a same-region internal transfer moves no
            -- data out of the account.
            SELECT CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE AS DAY,
                   COALESCE(TARGET_REGION, '(same region)') AS DEST,
                   SUM(BYTES_TRANSFERRED) AS BYTES
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -16, CURRENT_TIMESTAMP())
              AND (TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)
            GROUP BY 1, 2
        )
"""

# (V163 text, V169 text, slice or None, expected count of the NEW text in the slice / body)
_DELTAS: list[tuple[str, str, tuple[str, str] | None, int]] = [
    ("* :ai_credit_price AS MTD_USD,\n", f"* :ai_credit_price AS MTD_USD,\n{_MTD_ADD}", None, 2),
    ("               'MTD spend $' || ROUND(m.MTD_USD, 0) || ' is ' ||\n"
     "                   ROUND(m.MTD_USD / NULLIF(",
     "               'MTD spend through yesterday $' || ROUND(m.MTD_COMPLETE_USD, 0) || ' is ' ||\n"
     "                   ROUND(m.MTD_COMPLETE_USD / NULLIF(", _S08, 1),
    ("m.DAYS_IN_MONTH, 0) || '.',\n               m.MTD_USD,\n",
     "m.DAYS_IN_MONTH, 0) || ' for ' || (m.DAY_OF_MONTH - 1)\n"
     "                   || ' complete day(s); the partial metering of today is not counted.',\n"
     "               m.MTD_COMPLETE_USD,\n", _S08, 1),
    ("         AND m.MTD_USD > :budget_usd", "         AND m.MTD_COMPLETE_USD > :budget_usd", _S08, 1),
    ("ROUND(m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH), 0)",
     "ROUND(m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1), 0)", _S09, 1),
    ("               'MTD $' || ROUND(m.MTD_USD, 0) || ' + $' || ROUND(m.DAILY_RATE_USD, 0) ||\n"
     "                   '/day x ' || (m.DAYS_IN_MONTH - m.DAY_OF_MONTH) || ' remaining days.',\n"
     "               m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH),\n",
     "               'MTD through yesterday $' || ROUND(m.MTD_COMPLETE_USD, 0) || ' + $' || ROUND(m.DAILY_RATE_USD, 0) "
     "||\n                   '/day x ' || (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1) || ' remaining days incl. today.',\n"
     "               m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1),\n", _S09, 1),
    ("         AND (m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH))\n",
     "         AND (m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1))\n", _S09, 1),
    ("re-fire (cost-hunt6).\n",
     "re-fire (cost-hunt6).\n"
     "        -- V169 (R2-042, R2-103): TOTAL counts only once CONTRACT_START_DATE parses (the app twin\n"
     "        -- mart_sql.contract_exhaustion, r33); CONSUMED counts [start, CONTRACT_END_DATE) -- the end is "
     "EXCLUSIVE,\n"
     "        -- the app contract_pace clock -- and nothing raises once CURRENT_DATE() >= the end or when the "
     "projected\n"
     "        -- exhaustion falls on or after it. A blank end keeps the unbounded pre-V169 behaviour.\n", _S16, 1),
    ("            SELECT TOTAL, CONSUMED, DAILY_BURN,\n", "            SELECT TOTAL, CONSUMED, DAILY_BURN, TERM_END,\n",
     _S16, 1),
    ("                    (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0)\n",
     "                    (SELECT IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0,\n"
     "                                COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0))\n",
     _S16, 1),
    ("SETTINGS), CURRENT_DATE())) AS CONSUMED,\n",
     "SETTINGS), CURRENT_DATE())\n"
     "                       AND DAY < COALESCE(\n"
     "                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))\n"
     "                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), '9999-12-31'::DATE)) AS CONSUMED,\n"
     "                    (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))\n"
     "                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TERM_END,\n", _S16, 1),
    ("           AND p.DAYS_LEFT <= c.THRESHOLD_NUM\n",
     "           AND p.DAYS_LEFT <= c.THRESHOLD_NUM\n"
     "           AND (p.TERM_END IS NULL OR (CURRENT_DATE() < p.TERM_END AND p.EXHAUST_DATE < p.TERM_END))\n", _S16, 1),
    ("PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE", "PARTITION BY DATABASE_ID ORDER BY USAGE_DATE", _S12, 3),
    ("        JOIN (\n            SELECT DATABASE_NAME, USAGE_DATE,\n",
     "        JOIN (\n"
     "            -- V169 (R2-044): one series per LIVE database id. A dropped or re-created predecessor keeps\n"
     "            -- reporting rows (DELETED set) under the same DATABASE_NAME while its Time Travel and Fail-safe\n"
     "            -- bytes remain, and a by-name window paired the two ids on the same day arbitrarily. A re-created\n"
     "            -- database has no PREV on its first day and does not raise.\n"
     "            SELECT DATABASE_NAME, USAGE_DATE,\n", _S12, 1),
    ("CURRENT_DATE())\n            QUALIFY", "CURRENT_DATE())\n              AND DELETED IS NULL\n            QUALIFY",
     _S12, 1),
    ("AS TOP_METRICS\n", "AS TOP_METRICS,\n                   MAX(LATEST_LOAD) AS NEWEST_LOAD   -- V169 (R2-020/R2-043): "
     "the newest error cycle in the window\n", _S18, 1),
    ("               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())\n        FROM cfg c\n        JOIN r ON",
     "               c.RULE_ID || '|' || TO_VARCHAR(COALESCE(TO_DATE(r.NEWEST_LOAD), CURRENT_DATE()))   -- V169: one "
     "event per newest error-cycle day\n        FROM cfg c\n        JOIN r ON", _S18, 1),
    ("WHERE ENABLED\n        )\n        SELECT b.RULE_ID",
     f"WHERE ENABLED\n        ),\n{_XFER}        SELECT b.RULE_ID", _S19, 1),
    ("'Egress ' || eg.GB_24H || ' GB in 24h (14d avg '",
     "'Egress ' || eg.GB_DAY || ' GB on ' || TO_VARCHAR(eg.SPIKE_DAY) || ' (14d avg '", _S19, 1),
    ("               eg.GB_24H,\n", "               eg.GB_DAY,\n", _S19, 1),
    ("""            SELECT ROUND(SUM(IFF(START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP()),
                                 BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 1) AS GB_24H,
                   ROUND(SUM(BYTES_TRANSFERRED) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(TARGET_REGION, BYTES_TRANSFERRED) AS TOP_REGION
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
""", """            -- V169 (R2-047): the previous COMPLETE Central day. The scan runs ~06:50-07:30 Central, past the ~2h
            -- view latency, so consecutive daily windows tile the timeline (no blind slice before each scan, no
            -- task-jitter gap or overlap); the top destination is the largest per-region total of that day.
            SELECT MAX(DATEADD('day', -1, k.TODAY)) AS SPIKE_DAY,
                   ROUND(SUM(IFF(x.DAY = DATEADD('day', -1, k.TODAY), x.BYTES, 0)) / POWER(1024, 3), 1) AS GB_DAY,
                   ROUND(SUM(IFF(x.DAY >= DATEADD('day', -14, k.TODAY), x.BYTES, 0)) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(x.DEST, IFF(x.DAY = DATEADD('day', -1, k.TODAY), x.BYTES, NULL)) AS TOP_REGION
            FROM xfer x
            CROSS JOIN clk k
            WHERE x.DAY < k.TODAY
""", _S19, 1),
    ("         AND eg.GB_24H >= c.THRESHOLD_NUM\n", "         AND eg.GB_DAY >= c.THRESHOLD_NUM\n", _S19, 1),
    ("supersedes the MED one.)\n",
     "supersedes the MED one.)\n"
     "    --      V169 (R1-071): a NULL snapshot timer reads as 0 = never suspends (SHOW WAREHOUSES reports a\n"
     "    --      never-suspend warehouse as a NULL auto_suspend), like insights.show_auto_suspend and the mart "
     "loader.\n", _S24, 1),
    ("s.RECOVERABLE_CREDITS, w.AUTO_SUSPEND, w.SNAPSHOT_AT,",
     "s.RECOVERABLE_CREDITS, COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND, w.SNAPSHOT_AT,", _S24, 1),
    ("              AND w.AUTO_SUSPEND IS NOT NULL\n              AND (w.AUTO_SUSPEND <= 0 OR",
     "              AND (COALESCE(w.AUTO_SUSPEND, 0) <= 0 OR", _S24, 1),
    ("HIGH (c.SEVERITY).)\n",
     "HIGH (c.SEVERITY).)\n    --      V169 (R1-233): a THRESHOLD_NUM below 1 reads as 1 -- a regression is a rise; 0 "
     "raised every unchanged count.\n", _S29, 1),
    ("s.CUR_N - s.PRIOR_N >= COALESCE(c.THRESHOLD_NUM, 1)\n",
     "s.CUR_N - s.PRIOR_N >= GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)\n", _S29, 1),
    ("'alert scan daily v5 (V163: + COST_AI_USER_RUNAWAY + SEC_TRUST_REGRESSION, [07] burst-vs-lockout wording): '",
     "'alert scan daily v6 (V169: [08]/[09] complete-day MTD, [16] contract term, [12] live database id, [18] "
     "error-cycle day, [19] previous-day true egress, [24] NULL timer, [29] floor): '", None, 1),
]


def _reverse(d: str, deltas=None) -> str:
    for old, new, span, n in (deltas or _DELTAS):
        if span is None:
            assert d.count(new) == n, new[:90]
            d = d.replace(new, old)
        else:
            i = d.index(span[0])
            j = d.index(span[1], i)
            seg = d[i:j]
            assert seg.count(new) == n, new[:90]
            d = d[:i] + seg.replace(new, old) + d[j:]
    return d


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V169_OUT", "PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v169.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> tuple[str, str, str]:
    pre, pb, rp = tmp_path / "PF.sql", tmp_path / "PB.sql", tmp_path / "RP.sql"
    result = _run_gen(tmp_path, V169_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PART_B_OUT=str(pb),
                      REPAIR_OUT=str(rp))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG
    return pre.read_text(encoding="utf-8"), pb.read_text(encoding="utf-8"), rp.read_text(encoding="utf-8")


# -- generation ----------------------------------------------------------------------------------------------

def test_v169_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V169_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V169 drifted from its forward-generation -- edit outputs/gen_v169.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]


def test_v169_generator_reads_only_its_base():
    gen = read("outputs/gen_v169.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == ["V163__ai_runaway_trust_regression.sql"]
    assert "import app" not in gen and "from app" not in gen and gen.count(".read_text(") == 1


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v169_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    pre, part_b, _ = _extras(tmp_path)
    sql = pre if which == "preflight" else part_b
    code = _strip_noise(sql)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                   "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql and "\r" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = [p for p in sqlglot.parse(sql, dialect="snowflake") if p is not None]
    assert len(parsed) == (9 if which == "preflight" else 2)
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]
    if which == "preflight":
        for grid in (f"-- P169.{k} " for k in range(1, 8)):
            assert sql.count(grid) == 1, grid


def test_v169_preflight_carries_the_arm_text(tmp_path):
    pre, _, rp = _extras(tmp_path)
    a16, a24, a12 = _between(_D, *_S16), _between(_D, *_S24), _between(_D, *_S12)
    p16 = a16[a16.index("        JOIN (\n            SELECT TOTAL") + len("        JOIN "):a16.index(" p ON c.RULE_ID")]
    assert f"WITH p AS {p16}" in pre
    g12 = a12[a12.index("        JOIN (\n") + len("        JOIN "):a12.index(" g ON c.RULE_ID")]
    assert f"g_new AS {g12}" in pre
    chain = a24[a24.index("        WITH cfg AS (\n"):a24.index("        SELECT b.RULE_ID")]
    price = ("(SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) "
             "FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)")
    assert chain.replace(":credit_price", price).rstrip() in pre
    assert "AND (TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)" in pre
    assert not _strip_noise(rp).strip() and "Never DELETE an ALERT_EVENTS row" in rp


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v169_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard163 = _between(_V163, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard163.replace("-20163", "-20169").replace("'V163 requires V162 first", "'V169 requires V168 first")
            .replace("IF (v < 162)", "IF (v < 168)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert "not_ready EXCEPTION (-20169, 'V169 requires V168 first - apply migrations in order.');" in _MIG
    assert _MIG.count("EXECUTE IMMEDIATE") == 1 and "SELECT 169 AS VERSION" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 169);")
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "LATENCY:", "FIRST RUN:", "ROLLBACK:", "Apply AFTER V168. Idempotent; safe to re-run."):
        assert word in header, word


def test_v169_file_order_and_statements():
    from tests.test_migrations_parse import _plain_statements
    guard, mark = _MIG.index("EXCEPTION (-20169"), _MIG.index(_MARKER)
    daily = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    name = _MIG.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < mark < daily < name < version
    assert _MIG[mark + len(_MARKER):].startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG", "SET"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1 and _MIG.count("$$") == 4
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "DELETE", "MERGE", "VIEW",
                   "FUNCTION", "TABLE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert "ALERT_EVENTS" not in top and "\r" not in _MIG
    sqlglot = pytest.importorskip("sqlglot")
    for plain in _plain_statements(_MIG):
        (tree,) = sqlglot.parse(plain, dialect="snowflake")
        assert tree is not None


def test_v169_egress_name_refresh_is_guarded_on_the_seed_text():
    seed = "Outbound transfer above threshold (GB / 24h)"
    new = "True egress (cross-region or cross-cloud) above threshold GB on the previous complete Central day"
    assert f"'{seed}'" in read("snowflake/migrations/V043__task_retirement_alert_teeth.sql")
    stmt = (f"UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n   SET NAME = '{new}'\n"
            f" WHERE RULE_ID = 'COST_EGRESS_SPIKE'\n   AND NAME = '{seed}';\n")
    assert _MIG.count(stmt) == 1 and len(new) <= 200
    from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids
    assert "COST_EGRESS_SPIKE" in _config_enabled() & _raised_rule_ids()


def test_v169_description_fits():
    desc = re.search(r"SELECT 169 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and len(desc) <= 4000
    assert "re-derived from V163" in desc and "tally 14 unchanged" in desc


# -- lineage + round 13 ------------------------------------------------------------------------------------

def test_v169_marker_names_the_current_definer():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(169, "SP_ALERT_SCAN_DAILY")]
    assert r["src"] == "marker" and r["claims"] == [163] and r["prev"] == 163 and not r["waived"], r
    assert [v for v in _definers(texts)["SP_ALERT_SCAN_DAILY"] if 163 <= v <= 169] == [163, 169]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V169 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 1


def test_v169_daily_normalizes_back_to_v163_byte_for_byte():
    assert _reverse(_D) == _D163


@pytest.mark.parametrize("victim", ["AS DAILY_BURN", "'rule COST_SLEEP_POLLING - other rules unaffected'",
                                    "AND :budget_usd > 0", "COMPANY_FOR_DATABASE(g.DATABASE_NAME)",
                                    "LEFT(r.TOP_METRICS, 1700)", "'recon_scan_failed'"])
def test_v169_normalize_check_has_teeth(victim):
    assert _D.count(victim) >= 1
    assert _reverse(_D.replace(victim, victim[:-1], 1)) != _D163


@pytest.mark.parametrize("i", range(28))
def test_v169_every_declared_delta_is_real(i):
    assert len(_DELTAS) == 28
    assert _reverse(_D, [d for k, d in enumerate(_DELTAS) if k != i]) != _D163


# -- the deltas ----------------------------------------------------------------------------------------------

def test_v169_budget_pace_and_forecast_read_complete_days():
    assert _D.count("AS MTD_COMPLETE_USD,") == 2
    assert _D.count("m.MTD_COMPLETE_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM") == 1
    assert _D.count("m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1)") == 3
    for span in (_S08, _S09):
        code = _strip_noise(_between(_D, *span))
        assert "m.MTD_USD" not in code, span
        assert "DAY < CURRENT_DATE() AND NOT" in code and "AS MTD_COMPLETE_USD" in code
    assert "AND m.DAY_OF_MONTH > 1" in _between(_D, *_S08)
    # the R2-045 needles tests/test_r2_alerts_logic.py reads from the latest raiser bodies
    for needle in ("               m.MTD_COMPLETE_USD,\n",
                   "AND m.MTD_COMPLETE_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM",
                   "               m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1),\n",
                   "> :budget_usd * c.THRESHOLD_NUM", "               r.ERRORS,\n",
                   "r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)"):
        assert needle in _D, needle


def test_v169_contract_arm_gates_start_bounds_end_and_keeps_the_burn():
    a = _between(_D, *_S16)
    assert a.count("IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0,") == 1
    assert "AND DAY < COALESCE(" in a and "'CONTRACT_END_DATE'" in a and "'9999-12-31'::DATE" in a
    assert "(p.TERM_END IS NULL OR (CURRENT_DATE() < p.TERM_END AND p.EXHAUST_DATE < p.TERM_END))" in a
    assert "p.TOTAL > 0 AND p.DAILY_BURN > 0" in a and "IFF(p.DAYS_LEFT <= 14, 'CRITICAL', c.SEVERITY)" in a
    assert "               p.DAYS_LEFT,\n" in a                                         # METRIC_VALUE unchanged
    assert ("IFF(p.DAYS_LEFT <= 0, 'EXH', IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN')) || '|' || "
            "TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))") in a
    burn = "AS DAILY_BURN"
    assert _between(_D, "(SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)", burn) == \
        _between(_D163, "(SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)", burn)


def test_v169_storage_surge_reads_one_live_series_per_database_id():
    a = _between(_D, *_S12)
    assert a.count("PARTITION BY DATABASE_ID ORDER BY USAGE_DATE") == 3 and "PARTITION BY DATABASE_NAME" not in a
    assert "              AND DELETED IS NULL\n" in a and "AVG(" not in a
    for kept in ("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME)",
                 "g.DATABASE_NAME || ' grew ' || ROUND(g.GROWTH_GB, 1) || ' GB in a day'",
                 "c.RULE_ID || '|' || g.DATABASE_NAME || '|' || TO_VARCHAR(g.USAGE_DATE)",
                 "g.PREV_GB IS NOT NULL AND g.GROWTH_GB > c.THRESHOLD_NUM",
                 "WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())"):
        assert kept in a, kept


def test_v169_recon_key_is_the_newest_error_cycle_day():
    a = _between(_D, *_S18)
    assert "MAX(LATEST_LOAD) AS NEWEST_LOAD" in a
    assert "c.RULE_ID || '|' || TO_VARCHAR(COALESCE(TO_DATE(r.NEWEST_LOAD), CURRENT_DATE()))" in a
    assert "TO_VARCHAR(CURRENT_DATE())\n" not in a and "fails := fails + 1" not in a
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS();" in a
    # only [18]'s key moved: [19] keeps its own scan-day key
    k = "c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())"
    assert _D163.count(k) - _D.count(k) == 1 and k in _between(_D, *_S19)


def test_v169_egress_reads_the_previous_complete_central_day_of_true_egress():
    from app.data import security_sql
    a = _between(_D, *_S19)
    assert "DATEADD('hour', -24" not in a and "MAX_BY(TARGET_REGION, BYTES_TRANSFERRED)" not in a
    assert "x.DAY = DATEADD('day', -1, k.TODAY)" in a and "WHERE x.DAY < k.TODAY" in a
    assert "SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY" in a
    assert "MAX_BY(x.DEST, IFF(x.DAY = DATEADD('day', -1, k.TODAY), x.BYTES, NULL)) AS TOP_REGION" in a
    assert "GROUP BY 1, 2" in a
    # parity with the drill it points to: the SAME true-egress predicate, text for text
    pred = "AND (TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)"
    assert pred in a and pred in security_sql.egress_baseline()
    assert "COALESCE(TARGET_REGION, '(same region)')" in a and "COALESCE(TARGET_REGION, '(same region)')" in \
        security_sql.egress_baseline()
    assert "               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())\n" in a          # the key is unchanged
    assert "'. Source: DATA_TRANSFER_HISTORY - drill in Security -> Egress.'" in a


def test_v169_idle_arm_reads_a_null_timer_as_never_suspend():
    a = _between(_D, *_S24)
    assert "AND w.AUTO_SUSPEND IS NOT NULL" not in a
    assert a.count("COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND") == 1
    assert "AND (COALESCE(w.AUTO_SUSPEND, 0) <= 0 OR w.AUTO_SUSPEND > 60)" in a
    assert "GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, 60), 60), 3600)) AS TARGET_SEC" in a
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(s.WAREHOUSE_NAME) ORDER BY s.SNAPSHOT_AT DESC) = 1" in a


def test_v169_trust_floor_only_on_arm_29():
    a = _between(_D, *_S29)
    assert a.count("AND s.CUR_N - s.PRIOR_N >= GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)\n") == 1
    assert ">= COALESCE(c.THRESHOLD_NUM, 1)\n" not in _strip_noise(a)
    assert "g.N >= COALESCE(c.THRESHOLD_NUM, 1)" in _D and "r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)" in _D


def test_v169_leaves_every_other_block_alone():
    """test_v160 needs arm [25] byte-for-byte; the rest is pinned here."""
    for start, end in (("BEGIN\n", "    -- [08] COST_BUDGET_PACE"),
                       ("    -- [13b] COST_AI_CREEP", "    -- [16] COST_CONTRACT_BREACH"),
                       ("    -- [13] COST_SERVERLESS_CREEP", "    -- [19] COST_EGRESS_SPIKE"),
                       ("    -- [22] OPS_PIPELINE_DEGRADED", "    -- [24] COST_IDLE_OPPORTUNITY"),
                       ("    -- [25] COST_SLEEP_POLLING", "    -- [29] SEC_TRUST_REGRESSION"),
                       ("    -- [17] PIPE_REF_GAP", "    -- [18] DQ_RECON_ERROR"),
                       ("    IF (fails > 0) THEN", "    RETURN ")):
        assert _between(_D, start, end) == _between(_D163, start, end), start
    v160 = _proc(read("snowflake/migrations/V160__sleep_polling_alert.sql"), "SP_ALERT_SCAN_DAILY()")
    assert _between(v160, "    -- [25] COST_SLEEP_POLLING", "    -- [17] PIPE_REF_GAP") in _D
    assert "ct_hour" not in _D and "cadence gate" not in _D


def test_v169_tally_footprint_and_burn_history_lock():
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    assert len(_FAILS_INC_RE.findall(_D)) == 14
    self_alert_re, return_re = _SCAN_DENOMINATORS["SP_ALERT_SCAN_DAILY"]
    assert {int(x) for x in re.findall(self_alert_re, _D)} == {14}
    assert re.findall(return_re, _D) == [("14", "14")] * 3
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(_D))) == set(
        re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(_D163)))
    assert "$$" not in _body(_D) and "\\" not in _body(_D)
    # history_locks/test_rec10 reads the newest migration carrying 'AS DAILY_BURN': this one
    assert "/ NULLIF(COUNT(DISTINCT DAY), 0)" in _MIG and "DATEADD('day', -1, CURRENT_DATE())" in _MIG
    assert "COALESCE(SUM(CREDITS_BILLED), 0) / 30" not in _MIG


def test_v169_changed_arm_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    binds = {":budget_usd": "100", ":ai_credit_price": "2.2", ":credit_price": "3.68"}
    for span in (_S08, _S09, _S16, _S12, _S19, _S24, _S29, _S18):
        arm = _between(_D, *span)
        stmt = arm[arm.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):arm.index(";\n    EXCEPTION")]
        for k, v in binds.items():
            stmt = stmt.replace(k, v)
        (parsed,) = sqlglot.parse(stmt, dialect="snowflake")
        assert [c.name for c in parsed.this.expressions] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                             "METRIC_VALUE", "DEDUPE_KEY"], span
        (b,) = [s for s in parsed.find_all(exp.Subquery) if s.alias == "b"]
        assert len(b.this.expressions) == 7, span


# -- RUN_NEXT PART B ------------------------------------------------------------------------------------------
_PART_B_PRESENT = ("alert scan daily v6 (V169:", "AS MTD_COMPLETE_USD", "AS TERM_END", "PARTITION BY DATABASE_ID",
                   "AND DELETED IS NULL", "AS NEWEST_LOAD", "TARGET_CLOUD IS NOT NULL", "AS GB_DAY",
                   "COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND", "GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)",
                   "/14 rule blocks ok (daily)", "AS DAILY_BURN")
_PART_B_ABSENT = ("GB_24H", "AND w.AUTO_SUSPEND IS NOT NULL", "PARTITION BY DATABASE_NAME", "alert scan daily v5 (V163:")


def test_v169_part_b_get_ddl_fragments(tmp_path):
    body, body163 = _body(_D), _body(_D163)
    for frag in (*_PART_B_PRESENT, *_PART_B_ABSENT):
        assert not set(frag) & {"'", "\\", "\n", "\r"}, frag
    assert all(f in body for f in _PART_B_PRESENT) and not all(f in body163 for f in _PART_B_PRESENT)
    for frag in _PART_B_ABSENT:
        assert frag not in body and frag in body163, frag
    _, part_b, _ = _extras(tmp_path)
    ddl = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()')"
    for frag in _PART_B_PRESENT:
        assert f"IFF(CONTAINS({ddl}, '{frag}'), 'OK'" in part_b, frag
    for frag in _PART_B_ABSENT:
        assert f"IFF(NOT CONTAINS({ddl}, '{frag}'), 'OK'" in part_b, frag
    assert "'alert scan daily 14/14 rule blocks ok (daily)'" in part_b
