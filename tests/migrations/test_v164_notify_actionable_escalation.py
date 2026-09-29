"""Locks for V164 -- actionable Teams lines + a one-time CRITICAL escalation (Next-Fifty #40, owner 2026-09-29).

STRUCTURE + GENERATION + LOCKS; the executed eligibility model and the sqlite run of the capture live in
tests/migrations/test_v164_escalation_harness.py. What this file proves, from the shipped text:
  * generation -- outputs/gen_v164.py regenerates the migration byte-for-byte (LF only) and writes nothing else;
    the read-only PREFLIGHT section (P164.1-P164.4) and PART B grids are written only on request, parse, and the
    PREFLIGHT carries the proc's own line and capture text (binds swapped);
  * shape -- first line, the guard (-20164, v < 163, V160's shape), ALTER ADD COLUMN ESCALATED_AT, the
    WHEN NOT MATCHED SETTINGS seed, marker + SP_NOTIFY_WEBHOOK, the version row; nothing runs at apply time;
  * lineage + round 13 -- SP_NOTIFY_WEBHOOK is re-derived from V064 (its immediately previous definer), and
    cutting the escalation pass and reversing N1/N2/N4 gives V064's body back byte-for-byte (with teeth);
  * the deltas -- one line expression at all 5 sites (ASCII, never NULL, worst case far under 3000), every
    eligibility clause exactly once, capture-once, send -> audit -> stamp, failure isolation (no RAISE in the
    pass, the lease / tail / RETURN after it), the route_send_failed CONTEXT prefix the delivery card parses,
    no address and no SYSTEM$SEND_EMAIL, no DETAIL write, every escalation statement parses (sqlglot);
  * the RUN_NEXT PART B GET_DDL fragments; the config / Admin / docs lockstep this slice owns.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._source import ROOT, migration_tip, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V164__notify_actionable_lines_escalation.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V064 = read("snowflake/migrations/V064__webhook_drain_watermarks_alert_burn_telemetry.sql")
_V160 = read("snowflake/migrations/V160__sleep_polling_alert.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_P = _proc(_MIG, "SP_NOTIFY_WEBHOOK()")
_P64 = _proc(_V064, "SP_NOTIFY_WEBHOOK()")
_BODY = _P[_P.index("$$") + 2:_P.rindex("$$")]
_BLOCK_HEAD = "    -- V164 #40: CRITICAL ESCALATION PASS"
_TAIL_HEAD = "    -- Loud, not silent:"
_BLOCK = _between(_P, _BLOCK_HEAD, _TAIL_HEAD)

# ---------------------------------------------------------------------------------------------------
# Test-side copies (independent of outputs/gen_v164.py).
# ---------------------------------------------------------------------------------------------------
_MARKER = ("-- >>> derived:SP_NOTIFY_WEBHOOK  (from V064; actionable lines + one-time CRITICAL escalation pass, "
           "Next-Fifty #40, V164)\n")
_OLD_LINE = "'[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140)"
_FLAT = "TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   '))"
_NEW_LINE = ("'[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(" + _FLAT
             + ", '') = '', '', ' | ' || LEFT(" + _FLAT + ", 100)) || ' | event ' || e.EVENT_ID")
_ESC_PREFIX = "'ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now) || ' min) ' || "
_ESC_LINE = _ESC_PREFIX + _NEW_LINE
_DECLS = (
    "    esc_after_s VARCHAR;    -- V164 #40: SETTINGS.ESCALATE_AFTER_MIN (absent = '120'; 0 / not a number = off)\n",
    "    esc_after NUMBER DEFAULT 0;\n",
    "    esc_email VARCHAR;      -- V164 #40: SETTINGS.ESCALATE_EMAIL_INTEGRATION (absent = 'OVERWATCH_EMAIL'; '' = no "
    "email)\n",
    "    esc_now TIMESTAMP_NTZ;  -- V164 #40: one frozen clock for the whole escalation pass\n",
    "    esc_ids ARRAY;          -- V164 #40: the frozen escalation set (capture-once, the B9 invariant)\n",
    "    r_esc_ids ARRAY;        -- V164 #40: the part of esc_ids THIS route already delivered\n",
    "    esc_ok ARRAY;           -- V164 #40: ids at least one channel accepted (audited + stamped)\n",
    "    esc_msg VARCHAR;\n",
    "    esc_routes INT DEFAULT 0;\n",
    "    esc_emailed BOOLEAN DEFAULT FALSE;\n",
    "    escalated INT DEFAULT 0;\n",
    "    esc_note VARCHAR DEFAULT '';\n",
)
_C2 = ("    c2 CURSOR FOR           -- V164 #40: enabled routes, for the escalation re-post\n"
       "        SELECT r.ROUTE_ID, r.INTEGRATION_NAME\n"
       "        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r\n"
       "        WHERE r.ENABLED\n"
       "        ORDER BY r.ROUTE_ID;\n")
_RET_64 = "           ' newly expired-undelivered (event,route) pair(s) flagged';\n"
_RET_164 = ("           ' newly expired-undelivered (event,route) pair(s) flagged; ' || :escalated ||\n"
            "           ' CRITICAL(s) escalated' || :esc_note;\n")
# The capture's eligibility, one clause per line: each must appear EXACTLY once in the whole proc.
_CLAUSES = (
    "                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID\n"
    "                WHERE e.SEVERITY = 'CRITICAL'\n",
    "                  AND e.STATUS = 'OPEN'\n                  AND e.ACK_AT IS NULL\n",
    "                  AND e.ESCALATED_AT IS NULL\n",
    "                  AND e.RAISED_AT >= DATEADD('day', -7, :esc_now)\n",
    "                  AND COALESCE(e.NOTIFIED_AT, e.RAISED_AT) <= DATEADD('minute', -1 * :esc_after, :esc_now)\n",
    "                                  JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID\n"
    "                                  WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID\n"
    "                                    AND (i.ACK_AT IS NOT NULL OR i.STATUS <> 'OPEN'))\n",
    "                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT a\n"
    "                                  WHERE a.EVENT_ID = e.EVENT_ID AND a.ACTION = 'SNOOZE')\n",
    "                  AND (COALESCE(TRIM(:esc_email), '') <> ''\n"
    "                       OR EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n"
    "                                  JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r\n"
    "                                    ON r.ROUTE_ID = d.ROUTE_ID AND r.ENABLED\n"
    "                                  WHERE d.EVENT_ID = e.EVENT_ID))\n",
)
_SETTINGS_READ = (
    "        SELECT COALESCE(MAX(IFF(KEY = 'ESCALATE_AFTER_MIN', VALUE, NULL)), '120'),\n"
    "               COALESCE(MAX(IFF(KEY = 'ESCALATE_EMAIL_INTEGRATION', VALUE, NULL)), 'OVERWATCH_EMAIL')\n"
    "          INTO :esc_after_s, :esc_email\n"
    "          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS\n"
    "         WHERE KEY IN ('ESCALATE_AFTER_MIN', 'ESCALATE_EMAIL_INTEGRATION');\n"
    "        esc_after := COALESCE(TRY_TO_NUMBER(TRIM(:esc_after_s)), 0);\n"
    "        IF (esc_after <= 0) THEN\n"
    "            esc_note := '; escalation off (ESCALATE_AFTER_MIN)';\n"
)


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PREFLIGHT_OUT", "PART_B_OUT", "V164_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v164.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


# -- generation --------------------------------------------------------------------------------------------

def test_v164_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V164_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V164 drifted from its forward-generation -- edit outputs/gen_v164.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]       # no PREFLIGHT / PART B unless asked


def _extras(tmp_path: Path) -> tuple[str, str]:
    pf, pb = tmp_path / "pf.sql", tmp_path / "pb.sql"
    result = _run_gen(tmp_path, V164_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pf), PART_B_OUT=str(pb))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG      # the extras never change the migration
    return pf.read_text(encoding="utf-8"), pb.read_text(encoding="utf-8")


def _statements(sql: str) -> list[str]:
    from tests.test_migrations_parse import _split_statements
    out = []
    for stmt in _split_statements(sql):
        lines = [ln for ln in stmt.strip().splitlines() if ln.strip() and not ln.lstrip().startswith("--")]
        if lines:
            out.append("\n".join(lines))
    return out


_WRITE_WORDS = ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                "REVOKE", "EXECUTE", "USE")


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v164_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    sql = _extras(tmp_path)[0 if which == "preflight" else 1]
    code = _strip_noise(sql)
    for banned in _WRITE_WORDS:
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]", code), "a scripting :bind survived"
    assert "$$" not in sql and sql.isascii()
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", sql), "no address, ever"
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    stmts = _statements(sql)
    assert stmts
    for stmt in stmts:
        head = stmt.split(None, 1)[0].upper()
        parsed = sqlglot.parse_one(stmt, dialect="snowflake")
        if head in ("SHOW", "DESC"):
            continue                          # metadata reads (SHOW parses as a Command in sqlglot)
        assert parsed.key in ("select", "union"), (which, parsed.key, stmt[:80])
        writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
        assert not [type(n).__name__ for n in parsed.walk() if isinstance(n, writes)], stmt[:80]


def test_v164_preflight_sections_and_the_boolean_only_email_probe(tmp_path):
    pf = _extras(tmp_path)[0]
    heads = re.findall(r"^-- (P164\.\d)\b", pf, re.M)
    assert heads == ["P164.1", "P164.2", "P164.3", "P164.4"]
    # P164.1 returns booleans (and the owner role's NAME) only -- never a property value that could be an address
    p1 = _between(pf, "-- P164.1", "-- P164.2")
    assert "DESC INTEGRATION OVERWATCH_EMAIL;" in p1 and "SHOW GRANTS ON INTEGRATION OVERWATCH_EMAIL;" in p1
    selects = [s for s in _statements(p1) if s.startswith("SELECT")]
    assert len(selects) == 2
    for col in ("ENABLED", "DEFAULT_RECIPIENTS_SET", "DEFAULT_SUBJECT_SET", "ALLOWED_RECIPIENTS_SET",
                "SNOW_ACCOUNTADMINS_CAN_USE", "OWNER_ROLE"):
        assert f"AS {col}" in p1, col
    # every "property_value" read sits INSIDE a BOOLOR_AGG(...): nothing but a boolean leaves the grid
    rest = selects[0]
    while "BOOLOR_AGG(" in rest:
        i = rest.index("BOOLOR_AGG(") + len("BOOLOR_AGG(")
        depth, j = 1, i
        while depth:
            depth += {"(": 1, ")": -1}.get(rest[j], 0)
            j += 1
        rest = rest[:i - len("BOOLOR_AGG(")] + rest[j:]
    assert '"property' not in rest, rest
    assert '"property_value"' not in selects[1]
    # P164.3 counts spills against the proc's own max_batches
    assert "COUNT_IF(p.NEW_BATCHES > 6) AS RUNS_THAT_WOULD_SPILL" in pf
    assert "max_batches INT DEFAULT 6;" in _P


def test_v164_preflight_carries_the_proc_capture_and_line_verbatim(tmp_path):
    """P164.2 is generated from the SAME clause text as the proc's capture (binds -> k columns, the ESCALATED_AT
    line dropped because the column does not exist before the apply), so the census is what the proc will do."""
    pf = _extras(tmp_path)[0]
    swaps = ((":esc_now", "k.NOW_TS"), (":esc_after", "k.AFTER_MIN"), (":esc_email", "k.EMAIL_INTEGRATION"))
    for clause in _CLAUSES:
        want = clause
        for bind, col in swaps:
            want = want.replace(bind, col)
        if "ESCALATED_AT" in clause:
            assert want not in pf and "ESCALATED_AT" not in _strip_noise(_between(pf, "-- P164.2", "-- P164.3"))
        elif clause.startswith("                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c"):
            assert want.replace("\n                WHERE", "\n                CROSS JOIN k\n                WHERE") in pf
        else:
            assert want in pf, clause[:60]
    esc = _ESC_LINE.replace(":esc_now", "k.NOW_TS")
    assert f"SUM(LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({esc}, CHR(92)," in pf
    assert "AND k.AFTER_MIN > 0" in pf
    # the knobs parse exactly like the proc: absent -> '120' / 'OVERWATCH_EMAIL'; minutes via TRY_TO_NUMBER(TRIM())
    assert ("COALESCE(TRY_TO_NUMBER(TRIM(COALESCE(MAX(IFF(KEY = 'ESCALATE_AFTER_MIN', VALUE, NULL)), '120'))), 0)"
            " AS AFTER_MIN") in pf
    assert "COALESCE(MAX(IFF(KEY = 'ESCALATE_EMAIL_INTEGRATION', VALUE, NULL)), 'OVERWATCH_EMAIL') AS EMAIL_INTEGRATION" \
        in pf
    # P164.3 measures today's line against the V164 line
    p3 = _between(pf, "-- P164.3", "-- P164.4")
    assert f"LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({_OLD_LINE}, CHR(92)," in p3
    assert f"LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({_NEW_LINE}, CHR(92)," in p3


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v164_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert migration_tip() >= 164
    assert "not_ready EXCEPTION (-20164, 'V164 requires V163 first - apply migrations in order.');" in _MIG
    assert "IF (v < 163) THEN" in _MIG
    assert "SELECT 164 AS VERSION" in _MIG and "WHERE VERSION = 164);" in _MIG
    guard160 = _between(_V160, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard160.replace("-20160", "-20164").replace("'V160 requires V159 first", "'V164 requires V163 first")
            .replace("IF (v < 159)", "IF (v < 163)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 164);")
    head = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "LATENCY:", "FIRST RUN:", "ROLLBACK:", "!! OWNER SMOKE TEST",
                 "Apply AFTER V163. Idempotent; safe to re-run."):
        assert word in head, word


def test_v164_statement_order_and_nothing_runs_at_apply():
    guard = _MIG.index("EXCEPTION (-20164")
    alter = _MIG.index("ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS ESCALATED_AT "
                       "TIMESTAMP_NTZ;")
    seed = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t")
    mark = _MIG.index(_MARKER)
    proc = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < alter < seed < mark < proc < version
    assert _MIG[mark + len(_MARKER):].startswith("CREATE OR REPLACE PROCEDURE")    # the marker heads its proc
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1
    assert _MIG.count("$$") == 4                                                 # guard + one proc body
    outside = "".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0)      # scripting bodies out
    kinds = [s.split(None, 3)[:3] for s in _statements(outside)]
    assert kinds == [["EXECUTE", "IMMEDIATE"],
                     ["ALTER", "TABLE", "DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"],
                     ["MERGE", "INTO", "DBA_MAINT_DB.OVERWATCH.SETTINGS"],
                     ["CREATE", "OR", "REPLACE"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    top = _strip_noise(outside)
    for banned in ("CALL", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE", "USE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert _MIG.count("ALTER TABLE ") == 1 and "CREATE TABLE" not in _MIG and "CREATE OR REPLACE VIEW" not in _MIG


def test_v164_settings_seed_is_exact_and_when_not_matched_only():
    seed = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t", ";\n")
    assert seed.count("\n        ('") == 2
    assert "        ('ESCALATE_AFTER_MIN', '120'),\n" in seed
    assert "        ('ESCALATE_EMAIL_INTEGRATION', 'OVERWATCH_EMAIL')\n" in seed
    assert "WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE)" in seed and "WHEN MATCHED" not in seed
    assert "ALERT_CONFIG" not in _MIG[:_MIG.index(_MARKER)]                   # no rule seed: nothing is raised
    # the seeds are the app's code defaults, and the proc's absent-row fallbacks are the same values
    from app.config import DEFAULT_SETTINGS
    assert str(DEFAULT_SETTINGS["ESCALATE_AFTER_MIN"]) == "120"
    assert DEFAULT_SETTINGS["ESCALATE_EMAIL_INTEGRATION"] == "OVERWATCH_EMAIL"
    assert _SETTINGS_READ in _P


def test_v164_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 164 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc.replace("''", "")
    assert len(desc.replace("''", "'")) <= 4000
    assert desc.startswith("Next-Fifty #40: ") and "re-derived from V064" in desc
    assert desc.endswith("No task change, no procedure run at apply time.")


def test_v164_no_address_no_send_email_no_detail_write_and_ascii():
    for text in (_MIG, read("outputs/gen_v164.py"), read("snowflake/webhook_delivery.sql")):
        assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text), "an email address must never be committed"
    assert "SYSTEM$SEND_EMAIL" not in _MIG
    assert _MIG.isascii() and "\r" not in _MIG
    code = _strip_noise(_BODY)
    assert not re.search(r"\bDETAIL\s*=", code), "the append-only DETAIL is never written by the notifier"
    assert "SET DETAIL" not in _BODY
    # the only ALERT_EVENTS writes: V064's NOTIFIED_AT stamp and V164's ESCALATED_AT stamp
    sets = re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS e\s+SET (\w+) =", _BODY)
    assert sets == ["NOTIFIED_AT", "ESCALATED_AT"], sets
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" not in _BODY     # raises nothing (Guards A-E)
    # the email leg is the notification-integration send (DEFAULT_RECIPIENTS), never an address argument
    assert _BODY.count("SNOWFLAKE.NOTIFICATION.INTEGRATION(TRIM(:esc_email)));") == 1
    assert "EMAIL_INTEGRATION_CONFIG" not in _BODY


# -- lineage -------------------------------------------------------------------------------------------------

def test_v164_marker_names_the_current_definer():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(164, "SP_NOTIFY_WEBHOOK")]
    assert r["src"] == "marker" and r["claims"] == [64] and r["prev"] == 64 and not r["waived"], r
    defs = _definers(texts)
    assert [v for v in defs["SP_NOTIFY_WEBHOOK"] if v <= 164] == [12, 22, 26, 34, 63, 64, 164]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V164 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 1
    assert set(dict.fromkeys(re.findall(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.(\w+)", _MIG))) == {
        "SP_NOTIFY_WEBHOOK"}


# -- round 13: normalize-and-compare -------------------------------------------------------------------------

def _normalize(p: str) -> str:
    d = p
    i, j = d.index(_BLOCK_HEAD), d.index(_TAIL_HEAD)
    d = d[:i] + d[j:]                                                   # N3: the escalation pass
    decl = "".join(_DECLS)
    assert d.count(decl) == 1
    d = d.replace(decl, "")                                             # N1a
    assert d.count(_C2) == 1
    d = d.replace(_C2, "")                                              # N1b
    assert d.count(_NEW_LINE) == 2                                      # only the two drain sites remain
    d = d.replace(_NEW_LINE, _OLD_LINE)                                 # N2
    assert d.count(_RET_164) == 1
    return d.replace(_RET_164, _RET_64)                                 # N4


def test_v164_normalizes_back_to_v064_byte_for_byte():
    assert _normalize(_P) == _P64


@pytest.mark.parametrize("mutation", [
    ("max_batches INT DEFAULT 6;", "max_batches INT DEFAULT 10;"),                         # the drain bound
    ("ELSE DATEADD('hour', -24, CURRENT_TIMESTAMP()) END", "ELSE DATEADD('hour', -48, CURRENT_TIMESTAMP()) END"),
    ("       AND HOLDER = CURRENT_SESSION();\n", "       ;\n"),                            # the lease fence
    ("        RAISE;\n", "        RETURN 'x';\n"),                                         # the outer re-raise
    ("                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id)\n            ) f",
     "                                  WHERE d.EVENT_ID = e.EVENT_ID)\n            ) f"),   # the per-route ledger
])
def test_v164_normalize_has_teeth(mutation):
    old, new = mutation
    assert _P.count(old) >= 1, old
    mutated = _P.replace(old, new, 1)
    assert _normalize(mutated) != _P64


# -- the deltas ----------------------------------------------------------------------------------------------

def test_v164_one_line_expression_at_all_five_sites():
    assert _P.count(_NEW_LINE) == 5                    # drain fit + drain LISTAGG + escalation fit / re-post / email
    assert _P.count(_ESC_LINE) == 3
    assert _OLD_LINE + "," not in _P and _OLD_LINE + "\n" not in _P and _OLD_LINE + ")" not in _P
    drain = _P[:_P.index(_BLOCK_HEAD)]
    assert drain.count(_NEW_LINE) == 2 and _ESC_PREFIX not in drain
    assert "SELECT LISTAGG(" + _NEW_LINE + ", '\\n')" in drain                       # the message the drain sends
    assert "                           " + _NEW_LINE + ",\n                           CHR(92)," in drain   # its fit
    assert "SELECT LISTAGG(" + _ESC_LINE + ", '\\n')" in _BLOCK                      # the Teams re-post
    assert "SELECT LISTAGG(" + _ESC_LINE + ", CHR(10))" in _BLOCK                   # the email (plain text)
    assert "                           " + _ESC_LINE + ",\n                           CHR(92)," in _BLOCK
    assert _NEW_LINE.isascii() and "COMPANY" in _NEW_LINE and "' | event ' || e.EVENT_ID" in _NEW_LINE


def test_v164_drain_carry_overs_are_untouched():
    assert "max_batches INT DEFAULT 6;" in _P
    assert ("AND e.RAISED_AT >= CASE WHEN e.SEVERITY = 'CRITICAL'\n"
            "                                          THEN DATEADD('day', -7, CURRENT_TIMESTAMP())\n"
            "                                          ELSE DATEADD('hour', -24, CURRENT_TIMESTAMP()) END") in _P
    assert _P.count("DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE") == 3 and _P.count("RAISE;") == 1
    assert _P.endswith("        RAISE;\nEND;\n$$;")
    assert _P.count("message := REPLACE(:message, ") == 5
    assert "'OVERWATCH alerts:' || CHR(92) || 'n' || LEFT(:message, 3000)" in _P
    assert "AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')" in _P


def test_v164_every_eligibility_clause_appears_exactly_once():
    for clause in _CLAUSES:
        assert _P.count(clause) == 1, clause[:70]
    cap = _between(_BLOCK, "            SELECT ARRAY_AGG(f.EVENT_ID)", "            WHERE f.CUM_LEN <= 3000;")
    for clause in _CLAUSES:
        assert clause in cap, clause[:70]
    assert "INTO :esc_ids" in cap and cap.count("ORDER BY e.RAISED_AT ASC, e.EVENT_ID") == 1


def test_v164_settings_parse_and_off_switch():
    assert _SETTINGS_READ in _BLOCK
    assert "esc_now := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;" in _BLOCK
    assert _BLOCK.index("IF (esc_after <= 0) THEN") < _BLOCK.index("esc_now := CURRENT_TIMESTAMP()")
    # CURRENT_TIMESTAMP() only at the frozen clock and the stamp: the capture and every LISTAGG use :esc_now,
    # so the minutes in a line and the fit that sized it are the same number
    assert _BLOCK.count("CURRENT_TIMESTAMP()") == 2
    assert _BLOCK.count("DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now)") == 3
    assert _BLOCK.count(":esc_now") == 3 + 2       # the three line sites + the 7-day window + the clock bound


def test_v164_capture_once_send_then_audit_then_stamp():
    assert _BLOCK.count("INTO :esc_ids") == 1
    assert _BLOCK.count("                    WHERE d.ROUTE_ID = :r_route_id\n"
                        "                      AND ARRAY_CONTAINS(d.EVENT_ID::VARIANT, :esc_ids);") == 1
    teams = _BLOCK.index("SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));")
    email = _BLOCK.index("SNOWFLAKE.NOTIFICATION.INTEGRATION(TRIM(:esc_email)));")
    audit = _BLOCK.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)")
    stamp = _BLOCK.index("       SET ESCALATED_AT = CURRENT_TIMESTAMP()")
    assert teams < email < audit < stamp
    assert _BLOCK.count("CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(") == 2
    # a success adds its ids to esc_ok; the audit and the stamp touch only esc_ok ids still un-escalated
    assert "esc_ok := ARRAY_CAT(:esc_ok, :r_esc_ids);" in _BLOCK and "esc_ok := ARRAY_CAT(:esc_ok, :esc_ids);" in _BLOCK
    tail = _BLOCK[audit:]
    assert tail.count("WHERE e.ESCALATED_AT IS NULL\n") == 2
    assert tail.count("AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ok);") == 2
    assert "SELECT e.EVENT_ID, 'ESCALATE'," in tail and "'SP_NOTIFY_WEBHOOK'" in tail
    assert _BLOCK.index("IF (ARRAY_SIZE(:esc_ok) > 0) THEN") < audit
    assert "escalated := SQLROWCOUNT;" in tail
    # each success line sits right after its own send, inside its own BEGIN, before its handler
    for send, ok in (("INTEGRATION(:r_integration));", "esc_ok := ARRAY_CAT(:esc_ok, :r_esc_ids);"),
                     ("INTEGRATION(TRIM(:esc_email)));", "esc_ok := ARRAY_CAT(:esc_ok, :esc_ids);")):
        seg = _BLOCK[_BLOCK.index(send):]
        assert seg.index(ok) < seg.index("EXCEPTION")


def test_v164_escalation_pass_is_isolated_inside_the_lease():
    acquire = _P.index("       SET HELD = TRUE, HOLDER = CURRENT_SESSION()")
    drain_end = _P.index("    END FOR;\n\n" + _BLOCK_HEAD)
    block = _P.index(_BLOCK_HEAD)
    tail = _P.index("    SELECT 'NotifyWebhook', 'undelivered_expired',")
    release = _P.index("       SET HELD = FALSE, HOLDER = NULL")
    ret = _P.index("    RETURN 'sent ' || :sent_total")
    assert acquire < drain_end < block < tail < release < ret
    # one BEGIN ... EXCEPTION ... END for the whole pass; nothing inside re-raises
    code_lines = [ln for ln in _BLOCK.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]
    assert code_lines[0] == "    BEGIN" and code_lines[-1] == "    END;"
    assert sum(1 for ln in code_lines if ln == "    EXCEPTION") == 1
    outer = _BLOCK[_BLOCK.rindex("\n    EXCEPTION\n"):]
    assert "'escalation_failed'" in outer and "esc_note := '; escalation pass FAILED" in outer
    assert not re.search(r"\bRAISE\b", _strip_noise(_BLOCK))
    assert _BLOCK.rstrip().endswith("    END;")
    # the three handlers: re-post, email, whole pass
    assert _BLOCK.count("WHEN OTHER THEN") == 3


def test_v164_failure_logging_shapes():
    repost = _between(_BLOCK, "'route_send_failed'", "CURRENT_ROLE();")
    assert "'route ' || :r_route_id || ' integration ' || :r_integration ||" in repost
    assert "escalation re-post" in repost
    email = _between(_BLOCK, "'escalation_email_failed'", "CURRENT_ROLE();")
    assert "check DEFAULT_RECIPIENTS" in email and ":esc_email" in email
    assert _P.count(_RET_164) == 1
    # the delivery card attributes a route failure by SPLIT_PART(CONTEXT, ' ', 2): the re-post keeps the prefix
    from app.data import mart_sql
    card = mart_sql.last_delivery_health()
    assert "SPLIT_PART(CONTEXT, ' ', 2)" in card and "ERROR_TYPE = 'route_send_failed'" in card
    ctx = "route 1234-uuid integration OVERWATCH_WEBHOOK_TEAMS - escalation re-post; the email leg is unaffected"
    assert ctx.split(" ")[1] == "1234-uuid"


def test_v164_json_escape_is_v064s():
    v064 = re.findall(r"^            message := REPLACE\(:message, (.*)$", _P64, re.M)
    v164 = re.findall(r"^                        esc_msg := REPLACE\(:esc_msg, (.*)$", _BLOCK, re.M)
    assert len(v064) == 5 and v164 == v064
    assert ("'OVERWATCH ESCALATION - CRITICAL unacknowledged ' || :esc_after || '+ min:'\n"
            "                                    || CHR(92) || 'n' || LEFT(:esc_msg, 3000)),") in _BLOCK


# -- sqlglot: every escalation statement parses once the binds are literals ----------------------------------

def _code_keep_strings(sql: str) -> str:
    """Comments out, single-quoted literals ('' escapes) kept verbatim."""
    out, i, n = [], 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
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


def _split_on_semicolons(code: str) -> list[str]:
    """';' outside string literals ends a chunk (the escalation CONTEXT strings hold ';')."""
    parts, buf, in_str, i = [], [], False, 0
    while i < len(code):
        ch = code[i]
        buf.append(ch)
        if in_str:
            if ch == "'":
                if i + 1 < len(code) and code[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
        elif ch == ";":
            parts.append("".join(buf[:-1]).strip())
            buf = []
        i += 1
    return [p for p in parts if p]


_SQL_START = re.compile(r"^[ \t]*(SELECT|INSERT|UPDATE)\b", re.M)
_BINDS = {"esc_now": "CURRENT_TIMESTAMP()", "esc_after": "120", "esc_ids": "ARRAY_CONSTRUCT('a')",
          "r_esc_ids": "ARRAY_CONSTRUCT('a')", "esc_ok": "ARRAY_CONSTRUCT('a')", "r_route_id": "'r'",
          "esc_routes": "1", "esc_emailed": "TRUE", "emsg": "'m'", "r_integration": "'i'", "esc_email": "'e'",
          "esc_after_s": "'120'"}
_BIND_RE = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_STR_RE = re.compile(r"'(?:[^']|'')*'")


def _bind(sql: str) -> str:
    out, last = [], 0
    for m in _STR_RE.finditer(sql):
        out.append(_BIND_RE.sub(lambda b: _BINDS[b.group(1)], sql[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_BIND_RE.sub(lambda b: _BINDS[b.group(1)], sql[last:]))
    return "".join(out)


def _escalation_sql() -> list[str]:
    out = []
    for chunk in _split_on_semicolons(_code_keep_strings(_BLOCK)):
        m = _SQL_START.search(chunk)
        if m and not chunk[:m.start()].rstrip().endswith(":="):
            out.append(_bind(re.sub(r"\n[ \t]*INTO :[^\n]*", "", chunk[m.start():]).strip()))
    return out


def test_v164_every_escalation_statement_parses():
    sqlglot = pytest.importorskip("sqlglot")
    stmts = _escalation_sql()
    kinds = [s.split(None, 1)[0] for s in stmts]
    # settings read, capture, per-route set, re-post LISTAGG, re-post failure log, email LISTAGG, email failure
    # log, audit INSERT, stamp UPDATE, pass failure log
    assert kinds == ["SELECT", "SELECT", "SELECT", "SELECT", "INSERT", "SELECT", "INSERT", "INSERT", "UPDATE",
                     "INSERT"], kinds
    for stmt in stmts:
        assert not _BIND_RE.search(_STR_RE.sub("''", stmt)), stmt[:80]
        sqlglot.parse_one(stmt, dialect="snowflake")


def test_v164_parse_check_has_teeth():
    sqlglot = pytest.importorskip("sqlglot")
    broken = _escalation_sql()[1].replace("            ) f\n", "            f\n", 1)       # drop a paren
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(broken, dialect="snowflake")
    # the splitter is quote-aware: the email-failure CONTEXT holds a ';' and stays one statement
    assert any("' escalation(s) not emailed; check DEFAULT_RECIPIENTS" in s for s in _escalation_sql())


# -- RUN_NEXT PART B -------------------------------------------------------------------------------------------
# CONTAINS(GET_DDL('PROCEDURE', ...), '<frag>'): each fragment must be in the stored body, and quote-, backslash-
# and newline-free so it pastes into a SQL literal unchanged. _PART_B_ABSENT must be FALSE on V164, TRUE on V064.
_PART_B_PRESENT = (" | event ", "ESCALATED_AT", "escalation_failed", "escalation_email_failed",
                   "CRITICAL(s) escalated", "INTEGRATION(TRIM(:esc_email))", "ALERT_AUDIT a",
                   "max_batches INT DEFAULT 6")
_PART_B_ABSENT = ("LEFT(e.TITLE, 140),",)


def test_v164_part_b_get_ddl_fragments(tmp_path):
    body64 = _P64[_P64.index("$$") + 2:_P64.rindex("$$")]
    for frag in (*_PART_B_PRESENT, *_PART_B_ABSENT):
        assert not set(frag) & {"'", "\\", "\n", "\r"}, frag
    for frag in _PART_B_PRESENT:
        assert frag in _BODY, frag
    for frag in _PART_B_ABSENT:
        assert frag not in _BODY and frag in body64, frag
    assert not all(f in body64 for f in _PART_B_PRESENT)          # the grid tells V164 from V064
    pb = _extras(tmp_path)[1]
    for frag in _PART_B_PRESENT:
        assert f"CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), '{frag}')" in pb
    for frag in _PART_B_ABSENT:
        assert f"NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), '{frag}')" in pb
    for check in ("V164.1a", "V164.1b", "V164.1c", "V164.1d", "V164.2", "V164.3", "V164.4"):
        assert check in pb, check
    assert "CRITICAL(s) escalated" in pb and "TASK_NAME => 'TASK_ALERT_NOTIFY'" in pb


# -- the lockstep this slice owns (config, Admin, docs) ---------------------------------------------------------

def test_v164_settings_are_editable_and_seeded():
    from app.config import DEFAULT_SETTINGS
    from app.ui.pages import admin
    keys = list(DEFAULT_SETTINGS)
    i = keys.index("ESCALATE_AFTER_MIN")
    assert keys[i + 1] == "ESCALATE_EMAIL_INTEGRATION"                      # one '# Alert escalation' group
    assert DEFAULT_SETTINGS["ESCALATE_AFTER_MIN"] == 120
    assert admin._SETTING_EDITORS["ESCALATE_AFTER_MIN"] == ("number", {"min_value": 0.0, "step": 15.0})
    assert "ESCALATE_EMAIL_INTEGRATION" not in admin._SETTING_EDITORS       # the generic text input: blank = off
    cfg = read("app/config.py")
    assert cfg.index("# Alert escalation (V164") < cfg.index('"ESCALATE_AFTER_MIN": 120')


def test_v164_docs():
    wd = read("snowflake/webhook_delivery.sql")
    assert "V164: actionable lines" in wd and "ESCALATE_AFTER_MIN / ESCALATE_EMAIL_INTEGRATION" in wd
    assert "-- ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL SET" in wd
    assert "--     DEFAULT_RECIPIENTS = ('<recipient>')" in wd
    assert "<REDACTED-PASTE-IN-SNOWSIGHT>" in wd and "NEVER PASTE THE REAL URL INTO THIS FILE" in wd
    assert not re.search(r"^\s*ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL", wd, re.M)   # the recipe is commented
    doc = read("docs/EMAIL_RECIPIENT_RUNBOOK.md")
    assert "the recipient lives in exactly 4 requirements" in doc
    assert "4. **Escalation default recipients (V164)**" in doc and "DEFAULT_RECIPIENTS = ('<recipient>')" in doc
    assert "escalation_email_failed" in doc and "ESCALATE_EMAIL_INTEGRATION" in doc
    assert "three requirements" not in doc
    rb = read("RUNBOOK.md")
    row = [ln for ln in rb.splitlines() if ln.startswith("| TASK_ALERT_NOTIFY |")]
    assert len(row) == 1 and "ESCALATED_AT" in row[0] and "ESCALATE" in row[0]
    s19 = rb[rb.index("## §19 Microsoft Teams delivery"):rb.index("## §20")]
    for needle in ("Line format (V164)", "| event <EVENT_ID>", "CRITICAL escalation (V164)", "escalation_email_failed",
                   "escalation_failed", "ESCALATE_AFTER_MIN", "DEFAULT_RECIPIENTS"):
        assert needle in s19, needle
