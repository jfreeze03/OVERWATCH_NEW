"""Permanent parity lock: the COST_SLEEP_POLLING scan (SP_SCAN_SLEEP_POLLING, V160+) vs the app's ONE
definition of a sleep, its billing basis and its owner / next-step copy.

outputs/gen_v160.py never imports app/: it carries LITERAL copies of system_wait.SLEEP_SQL_PATTERN (each LF
written as the two-character escape \\n, so the literal sits on one physical line and survives CRLF),
SLEEP_EXCLUDED_TYPE_PREFIXES and cs_driver's two sleep next-step texts. These locks read the LATEST definer
body across every V*.sql (last CREATE OR REPLACE wins, as on the live account), so a change to the app's
pattern, prefixes or copy fails here until the proc is forward re-derived -- and so does a later
re-derivation that drifts from the app. The predicate is compared, normalized, with the SLEEP_FLAG of the
v4.595 Spend panel (mart_sql.cloud_svc_billed_families) the alert pushes: both must agree on every family.
"""

from __future__ import annotations

import functools
import re
import sqlite3

import pytest

from app.data import mart_sql
from app.logic import cs_driver
from app.logic.system_wait import SLEEP_EXCLUDED_TYPE_PREFIXES, SLEEP_SQL_PATTERN

_PROC = "SP_SCAN_SLEEP_POLLING"


@functools.cache
def _body() -> str:
    from tests.test_alert_rule_consistency import _latest_proc_bodies

    bodies = _latest_proc_bodies()
    assert _PROC in bodies, f"{_PROC} has no definer in snowflake/migrations"
    return bodies[_PROC]


@functools.cache
def _panel() -> str:
    return mart_sql.cloud_svc_billed_families(7)


# --- a small Snowflake-aware scanner: literals first, then -- / // / /* */ comments --------------------------
def _skip_literal(sql: str, i: int) -> int:
    """``sql[i]`` is an opening quote -> the index just past the closing quote ('' and \\x escapes)."""
    j, n = i + 1, len(sql)
    while j < n:
        if sql[j] == "\\":
            j += 2
            continue
        if sql[j] == "'":
            if sql[j + 1:j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    raise AssertionError(f"unterminated literal at {i}: {sql[i:i + 60]!r}")


def _tokens(sql: str) -> list[tuple[str, str]]:
    """-> [(kind, text)]: 'code', 'str' (the RAW literal between its quotes) or 'comment'."""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    def flush() -> None:
        if buf:
            out.append(("code", "".join(buf)))
            buf.clear()

    while i < n:
        two = sql[i:i + 2]
        if two in ("--", "//"):
            flush()
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", sql[i:j]))
            i = j
        elif two == "/*":
            flush()
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(("comment", sql[i:j]))
            i = j
        elif sql[i] == "'":
            flush()
            j = _skip_literal(sql, i)
            out.append(("str", sql[i + 1:j - 1]))
            i = j
        else:
            buf.append(sql[i])
            i += 1
    flush()
    return out


_SF_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0",
               "\\": "\\", "'": "'", '"': '"'}


def _sf_decode(raw: str) -> str:
    """A Snowflake single-quoted literal's RAW text -> its value ('' -> ', backslash escapes)."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "'":
            assert raw[i + 1:i + 2] == "'", raw
            out.append("'")
            i += 2
        elif ch == "\\":
            nxt = raw[i + 1]
            assert nxt in _SF_ESCAPES, f"unexpected escape \\{nxt} in {raw!r}"
            out.append(_SF_ESCAPES[nxt])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _close(sql: str, i: int) -> int:
    """``sql[i] == '('`` -> the index of its matching ')' (literals and comments skipped)."""
    assert sql[i] == "(", sql[i:i + 40]
    depth, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two in ("--", "//"):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if two == "/*":
            i = sql.index("*/", i + 2) + 2
            continue
        ch = sql[i]
        if ch == "'":
            i = _skip_literal(sql, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError("unbalanced parentheses")


def _args(sql: str, open_idx: int) -> list[str]:
    """The top-level comma-separated arguments of the call whose '(' is at ``open_idx``."""
    inner = sql[open_idx + 1:_close(sql, open_idx)]
    parts: list[str] = []
    depth, start, i = 0, 0, 0
    while i < len(inner):
        ch = inner[i]
        if ch == "'":
            i = _skip_literal(inner, i)
            continue
        if inner[i:i + 2] == "--":
            j = inner.find("\n", i)
            i = len(inner) if j < 0 else j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
        i += 1
    parts.append(inner[start:])
    return [p.strip() for p in parts]


def _cte(sql: str, name: str) -> str:
    m = re.search(rf"\b{name} AS \(", sql)
    assert m, f"CTE {name} not found"
    o = m.end() - 1
    return sql[o + 1:_close(sql, o)]


def _code_only(sql: str) -> str:
    """Comments dropped, literals kept verbatim."""
    return "".join(t if k == "code" else f"'{t}'" for k, t in _tokens(sql) if k != "comment")


def _normalized(expr: str) -> tuple[str, tuple[str, ...]]:
    """-> (code skeleton with each literal as ?, whitespace-normalized; the DECODED literal values)."""
    skel: list[str] = []
    lits: list[str] = []
    for kind, text in _tokens(expr):
        if kind == "str":
            skel.append("?")
            lits.append(_sf_decode(text))
        elif kind == "code":
            skel.append(text)
    s = re.sub(r"\s+", " ", "".join(skel)).strip()
    s = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", s))
    return s, tuple(lits)


def _sleepfam_where() -> str:
    fam = _cte(_body(), "sleepfam")
    return fam[fam.index("\n        WHERE ") + len("\n        WHERE "):]


def _panel_sleep_flag() -> str:
    tagged = _cte(_panel(), "tagged")
    i = tagged.index("IFF(f.QUERY_PARAMETERIZED_HASH")
    args = _args(tagged, i + 3)
    assert args[1:] == ["1", "0"] and tagged[_close(tagged, i + 3):].startswith(") AS SLEEP_FLAG"), args
    return args[0]


# ------------------------------------------------------------------------------------------------------------
def test_reads_the_latest_definer_and_it_is_not_vacuous():
    body = _body()
    assert body.startswith(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{_PROC}(FORCE_RUN BOOLEAN)")
    assert body.rstrip().endswith("$$;")
    for need in ("sleepfam AS (", "REGEXP_INSTR(UPPER(f.SAMPLE_TEXT)", "'COST_SLEEP_POLLING|'",
                 "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):
        assert need in body, need


def test_regexp_instr_literal_is_the_app_pattern_with_each_lf_escaped():
    fam = _cte(_body(), "sleepfam")
    calls = [m.end() - 1 for m in re.finditer(r"REGEXP_INSTR\(", fam)]
    assert len(calls) == 1, "sleepfam must decide the sleep with exactly one REGEXP_INSTR"
    subject, lit = _args(fam, calls[0])
    assert subject == "UPPER(f.SAMPLE_TEXT)"
    assert lit.startswith("'") and lit.endswith("'")
    raw = lit[1:-1]
    assert raw == SLEEP_SQL_PATTERN.replace("\n", "\\n")
    assert _sf_decode(raw) == SLEEP_SQL_PATTERN                      # Snowflake reads back the app pattern
    # one physical line (CRLF-proof), nothing a literal would reinterpret beyond the LF escapes
    assert "\n" not in raw and "\r" not in raw and "'" not in raw
    assert set(re.findall(r"\\(.)", raw)) == {"n"}
    assert raw.count("\\n") == SLEEP_SQL_PATTERN.count("\n") == 4
    assert _body().count(raw) == 1
    assert any(raw in line and "REGEXP_INSTR(" in line for line in _body().splitlines())


def test_not_like_prefixes_are_the_app_exclusions():
    where = _sleepfam_where()
    found = re.findall(r"f\.QUERY_TYPE NOT LIKE '([^']*)'", where)
    assert found == [p + "%" for p in SLEEP_EXCLUDED_TYPE_PREFIXES]
    assert len(set(found)) == len(found) == len(SLEEP_EXCLUDED_TYPE_PREFIXES)
    assert "f.QUERY_PARAMETERIZED_HASH <> 'n/a'" in where


def test_sleepfam_predicate_is_the_panel_sleep_flag_normalized():
    ours = _normalized(_sleepfam_where())
    panel = _normalized(_panel_sleep_flag())
    assert ours == panel
    skeleton, lits = ours
    assert skeleton.startswith("f.QUERY_PARAMETERIZED_HASH <> ? AND ")
    assert skeleton.endswith("AND REGEXP_INSTR(UPPER(f.SAMPLE_TEXT), ?) > 0")
    assert lits[-1] == SLEEP_SQL_PATTERN and lits[0] == "n/a"
    # same subject: the family-grain window-MIN sample, grouped exactly like the panel's family
    grain = "GROUP BY fd.QUERY_PARAMETERIZED_HASH, fd.QUERY_TYPE, fd.WAREHOUSE_NAME, fd.USER_NAME"
    assert grain in _cte(_body(), "fam") and grain in _cte(_panel(), "fam")
    assert "MIN(fd.SAMPLE_TEXT) AS SAMPLE_TEXT" in _cte(_body(), "fam")
    assert "MIN(fd.SAMPLE_MIN) AS SAMPLE_TEXT" in _cte(_panel(), "fam")
    assert "FROM fam f" in _cte(_body(), "sleepfam") and "FROM fam f" in _cte(_panel(), "tagged")


def test_call_text_regex_is_the_sleep_call_shape():
    fam = _cte(_body(), "sleepfam")
    i = fam.index("REGEXP_SUBSTR(") + len("REGEXP_SUBSTR")
    subject, lit = _args(fam, i)
    assert subject == "UPPER(f.SAMPLE_TEXT)"
    shape = SLEEP_SQL_PATTERN[SLEEP_SQL_PATTERN.index("(SELECT|CALL)"):]
    assert _sf_decode(lit[1:-1]).startswith(shape), (lit, shape)


def test_billing_basis_is_the_panel_basis():
    body, panel = _body(), _panel()
    billed = "SUM(COALESCE(x.CREDITS_CLOUD_SVCS, 0) + COALESCE(x.CREDITS_ADJUSTMENT, 0)) AS CS_BILLED_DAY"
    assert billed in _cte(body, "bill") and billed in _cte(panel, "bill")
    cap = r"LEAST\(\w+\.\w+, GREATEST\(0, b\.CS_BILLED_DAY\)\)"
    assert re.search(cap, _cte(body, "pb")) and re.search(cap, _cte(body, "acct"))
    assert re.search(cap, _cte(panel, "sleepsum"))
    # complete days only: the newest FACT_METERING_DAILY row (the UTC day in progress) is never priced
    assert "x.DAY < (SELECT MAX(z.DAY) FROM" in _cte(panel, "bill")
    assert "x.DAY < :newest" in _cte(body, "bill")
    assert "(SELECT MAX(z.DAY) AS NEWEST FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY z) mx" in body


@pytest.mark.parametrize("attr", ["_SLEEP_TASK_ACTION", "_SLEEP_CLIENT_ACTION"])
def test_next_step_texts_are_the_cs_driver_copy_with_quotes_doubled(attr):
    text = getattr(cs_driver, attr)
    assert "'" in text                                      # the doubling is exercised ("won't")
    body = _body()
    assert body.count(text.replace("'", "''")) == 1
    assert text not in body                                 # never an undoubled (broken) literal


def test_next_step_branch_order_matches_remediation_action():
    body = _code_only(_body())
    i = body.index("IFF(UPPER(o.USER_NAME) = 'SYSTEM',")
    cond, task, client = _args(body, i + 3)
    assert cond == "UPPER(o.USER_NAME) = 'SYSTEM'"
    assert _sf_decode(task[1:-1]) == cs_driver._SLEEP_TASK_ACTION
    assert _sf_decode(client[1:-1]) == cs_driver._SLEEP_CLIENT_ACTION
    sleep = cs_driver.SLEEP_POLLING
    assert cs_driver.remediation_action(sleep, {"USER_NAME": "SYSTEM"}) == cs_driver._SLEEP_TASK_ACTION
    assert cs_driver.remediation_action(sleep, {"USER_NAME": "CTM_SVC"}) == cs_driver._SLEEP_CLIENT_ACTION


def _owner_hint_sql() -> str:
    body = _code_only(_body())
    i = body.index("IFF(UPPER(a.USER_NAME) = 'SYSTEM',")
    return body[i:_close(body, i + 3) + 1]


def test_owner_hint_fragments_match_cs_driver_format():
    expr = _owner_hint_sql()
    for frag in ("'Task owner (role '", "')'", "'Task owner'", "' · '", "'User '"):
        assert frag in expr, frag
    assert "IFF(UPPER(d.USER_NAME) = 'SYSTEM', d.ROLE_NAME, '') AS TASK_ROLE" in _body()
    # the Python side really is built from those fragments
    assert cs_driver.owner_hint({"USER_NAME": "SYSTEM", "ROLE_NAME": "R1"}) == "Task owner (role " + "R1" + ")"
    assert cs_driver.owner_hint({"USER_NAME": "SYSTEM", "ROLE_NAME": ""}) == "Task owner"
    assert cs_driver.owner_hint({"USER_NAME": "U1", "USER_TOP_APP": "App"}) == "App" + " · " + "U1"
    assert cs_driver.owner_hint({"USER_NAME": "U1"}) == "User " + "U1"


@pytest.mark.parametrize("user,task_role,app", [
    ("SYSTEM", "TRXS_TASK_OWNER", None),
    ("system", "TRXS_TASK_OWNER", "Snowsight"),          # a task: the app never applies
    ("SYSTEM", "", None),
    ("CTM_SVC", "", "Control-M"),
    ("BOB", "", None),
    ("BOB", "", ""),
])
def test_owner_hint_sql_twin_evaluates_like_cs_driver(user, task_role, app):
    """The SQL twin, EXECUTED (sqlite; IFF shimmed) on the census's inputs: TASK_ROLE is ROLE_NAME for a
    SYSTEM row and '' otherwise; USER_TOP_APP is NULL when the user has no known application."""
    con = sqlite3.connect(":memory:")
    try:
        con.create_function("IFF", 3, lambda c, a, b: a if c else b, deterministic=True)
        got = con.execute(f"SELECT {_owner_hint_sql()} FROM (SELECT ? AS USER_NAME, ? AS TASK_ROLE) a "
                          "CROSS JOIN (SELECT ? AS USER_TOP_APP) ap", (user, task_role, app)).fetchone()[0]
    finally:
        con.close()
    role = task_role if user.upper() == "SYSTEM" else ""
    assert got == cs_driver.owner_hint({"USER_NAME": user, "ROLE_NAME": role, "USER_TOP_APP": app})
