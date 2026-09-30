"""Permanent parity: app/logic/digest_grounding <-> the LATEST SP_DAILY_DIGEST body (V165 onward).

outputs/gen_v165.py copies the grounding literals into the proc without importing app/, so a change on either side
must fail here and force a forward re-derivation. Two layers:
  * literals -- every pattern appears quoted in the proc; the strips run in the same order with the same flags;
    the 11 noun branches, the scale words and the percent words match in order; the tolerance is ai_grounding's;
  * behaviour -- the proc's OWN matching SELECT (the unit / noun / tolerance join, GROUP BY per figure) runs in
    sqlite over tokens cut with the proc's own patterns, and agrees with check_digest on a corpus of passing and
    failing drafts. (Only REGEXP_SUBSTR_ALL / FLATTEN are replaced -- by Python re over the proc's literals -- the
    engine difference PREFLIGHT P165.2 / P165.3 check on the live account.)
Reads the latest body through tests.test_alert_rule_consistency._latest_proc_bodies (last definition wins).
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from app.logic import ai_grounding
from app.logic import digest_grounding as dg
from tests.test_alert_rule_consistency import _latest_proc_bodies

_PROC = _latest_proc_bodies()["SP_DAILY_DIGEST"]
_BODY = _PROC[_PROC.index("$$") + 2:_PROC.rindex("$$")]

FACTS = ("WINDOW_DAYS=7; SPEND_USD=12345.67; CREDITS=3354.80; QUERIES=1234567; FAILED_QUERIES=321; "
         "FAILED_QUERY_PCT=0.03; QUERY_SUCCESS_PCT=99.97; QUEUED_MINUTES=12.3; SPILL_GB=4.56; TASK_RUNS=900; "
         "TASK_FAILURES=3; TASK_FAILURE_PCT=0.33; TASK_SUCCESS_PCT=99.67; ALERT_WINDOW_HOURS=24; "
         "OPEN_CRITICAL_ALERTS=0; OPEN_HIGH_ALERTS=2; ALERTS_RAISED_24H=5")

_STRIP_RE = re.compile(r"clean := REGEXP_REPLACE\(:(?:body|clean), '([^']*)', ' '(?:, 1, 0, '(\w+)')?\);")


# -- literals ------------------------------------------------------------------------------------------------------

def test_the_latest_digest_proc_is_the_measured_one():
    assert "REGEXP_SUBSTR_ALL(:clean," in _BODY and "grounding_ok := (n_bad = 0);" in _BODY


def test_every_pattern_literal_is_in_the_proc():
    for pat, _flags in dg.DIGEST_STRIP_PATTERNS:
        assert f"'{pat}'" in _BODY, pat
    for pat in (dg.DIGEST_FIGURE_PATTERN, dg.DIGEST_NUM_PATTERN, dg.DIGEST_WORD_PATTERN, dg.DIGEST_FACT_PATTERN):
        assert f"'{pat}'" in _BODY, pat
    assert f"REGEXP_SUBSTR_ALL(:clean,\n                             '{dg.DIGEST_FIGURE_PATTERN}', 1, 1, 'i')" in _BODY
    assert f"REGEXP_SUBSTR_ALL(:facts, '{dg.DIGEST_FACT_PATTERN}')" in _BODY
    assert f"REGEXP_SUBSTR(x.VALUE::VARCHAR, '{dg.DIGEST_NUM_PATTERN}') AS NUM" in _BODY
    assert f"LOWER(REGEXP_SUBSTR(x.VALUE::VARCHAR, '{dg.DIGEST_WORD_PATTERN}')) AS WORD" in _BODY
    for pat in (*(p for p, _ in dg.DIGEST_STRIP_PATTERNS), dg.DIGEST_FIGURE_PATTERN, dg.DIGEST_FACT_PATTERN):
        assert "\\" not in pat and "'" not in pat                  # valid unchanged in a $$ body


def test_strips_run_in_the_same_order_with_the_same_flags():
    got = [(pat, flags or "") for pat, flags in _STRIP_RE.findall(_BODY)]
    assert got == list(dg.DIGEST_STRIP_PATTERNS)
    first = _STRIP_RE.search(_BODY).group(0)
    assert first.startswith("clean := REGEXP_REPLACE(:body,")          # the draft in, the stripped copy out


def test_noun_branches_match_in_order():
    branches = re.findall(r"WHEN w\.WORD (LIKE|=) '([^']+)' THEN '([^']+)'", _BODY)
    assert len(branches) == 11 == len(dg.DIGEST_KEYWORDS)
    for (op, pat, key), (want_pat, want_key) in zip(branches, dg.DIGEST_KEYWORDS, strict=True):
        assert (pat, key) == (want_pat, want_key)
        assert op == ("LIKE" if want_pat.endswith("%") else "=")


def test_scale_and_percent_words_match():
    scale = re.findall(r"WHEN w\.WORD IN \(([^)]*)\) THEN (\d+)", _BODY)
    got = tuple((tuple(re.findall(r"'([^']+)'", words)), int(n)) for words, n in scale)
    assert got == dg.DIGEST_SCALE_WORDS
    pct = re.search(r"WHEN CONTAINS\(w\.TOK, '%'\) OR w\.WORD IN \(([^)]*)\) THEN 'pct'", _BODY).group(1)
    assert tuple(re.findall(r"'([^']+)'", pct)) == dg.DIGEST_PCT_WORDS
    assert "WHEN STARTSWITH(w.TOK, '$') THEN 'usd'" in _BODY


def test_tolerance_is_ai_groundings():
    assert dg.DIGEST_REL_TOL == ai_grounding._REL_TOL == 0.005
    assert "GREATEST(0.5 * POWER(10, -u.DECIMALS) * u.SCALE, 0.005 * u.NUM_VAL * u.SCALE) AS TOL" in _BODY
    # W3 (review r1): the inclusive half step, with a relative slack for DOUBLE noise -- the same factor both sides
    assert dg.DIGEST_TOL_SLACK == 1.000000001
    assert f"AND ABS(f.FVAL - t.VAL) <= t.TOL * {dg.DIGEST_TOL_SLACK!r}\n" in _BODY
    assert "(t.UNIT = 'usd' AND ENDSWITH(f.FKEY, '_USD'))" in _BODY
    assert "(t.UNIT = 'pct' AND ENDSWITH(f.FKEY, '_PCT'))" in _BODY
    assert "AND (t.KEYWORD IS NULL OR CONTAINS(f.FKEY, t.KEYWORD))" in _BODY


# -- behaviour: the proc's own matching SELECT in sqlite -----------------------------------------------------------

def _try_double(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_part(s, d, n):
    if s is None or d is None:
        return None
    parts = str(s).split(d)
    return parts[n - 1] if 0 < n <= len(parts) else ""


def _regexp_substr(s, pat):
    if s is None:
        return None
    m = re.search(pat, s)
    return m.group() if m else None


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("TRY_TO_DOUBLE", 1, _try_double)
    con.create_function("CONTAINS", 2, lambda a, b: None if a is None or b is None else int(b in a))
    con.create_function("STARTSWITH", 2, lambda a, b: None if a is None or b is None else int(a.startswith(b)))
    con.create_function("ENDSWITH", 2, lambda a, b: None if a is None or b is None else int(a.endswith(b)))
    con.create_function("SPLIT_PART", 3, _split_part)
    con.create_function("GREATEST", 2, lambda a, b: None if a is None or b is None else max(a, b))
    con.create_function("POWER", 2, lambda a, b: None if a is None or b is None else float(a) ** float(b))
    con.create_function("REGEXP_SUBSTR", 2, _regexp_substr)
    con.create_function("LEFT", 2, lambda s, n: None if s is None else s[:n])
    con.execute('CREATE TABLE xtab ("INDEX" INTEGER, VALUE TEXT)')
    con.execute("CREATE TABLE ptab (VALUE TEXT)")
    return con


def _count(pattern: str, text: str, n: int = 1) -> None:
    assert len(re.findall(pattern, text)) == n, pattern


def _ground_select() -> tuple[str, str, str]:
    """(sqlite SQL, figure pattern, fact pattern): the proc's grounding SELECT with only REGEXP_SUBSTR_ALL /
    FLATTEN, the INTO clause, ::VARCHAR, LISTAGG and the INDEX keyword translated (each count-asserted)."""
    start = _BODY.index("        SELECT COUNT(*), COUNT_IF(NOT g.MATCHED),")
    g = _BODY[start:_BODY.index("        ) g;", start) + len("        ) g")]
    fig_re = r"TABLE\(FLATTEN\(INPUT => REGEXP_SUBSTR_ALL\(:clean,\s*'([^']*)', 1, 1, 'i'\)\)\) x"
    fact_re = r"TABLE\(FLATTEN\(INPUT => REGEXP_SUBSTR_ALL\(:facts, '([^']*)'\)\)\) p"
    figure, fact = re.search(fig_re, g).group(1), re.search(fact_re, g).group(1)
    for pat in (fig_re, fact_re, r"\n\s*INTO :n_checked, :n_bad, :ungrounded",
                r"LISTAGG\((.*?), ', '\) WITHIN GROUP \(ORDER BY g\.POS\)", r"COUNT_IF\(NOT g\.MATCHED\)"):
        _count(pat, g)
    g = re.sub(fig_re, "xtab x", g)
    g = re.sub(fact_re, "ptab p", g)
    g = re.sub(r"\n\s*INTO :n_checked, :n_bad, :ungrounded", "", g)
    g = re.sub(r"LISTAGG\((.*?), ', '\) WITHIN GROUP \(ORDER BY g\.POS\)", r"GROUP_CONCAT(\1, ', ')", g)
    # Snowflake COUNT_IF is COUNT over the condition: 0 on no rows (a draft with no figures is grounded);
    # PREFLIGHT P165.2 proves it live (COUNT_IF_ON_EMPTY)
    g = g.replace("COUNT_IF(NOT g.MATCHED)", "COUNT(CASE WHEN NOT g.MATCHED THEN 1 END)")
    assert g.count("::VARCHAR") == 5 and g.count("x.INDEX") == 1
    g = g.replace("::VARCHAR", "").replace("x.INDEX", 'x."INDEX"')
    assert ":" not in _literals_out(g), "a bind survived"
    return g, figure, fact


def _literals_out(sql: str) -> str:
    """String literals blanked, so a ':' inside a pattern is not taken for a bind."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def _sql_check(body: str, facts: str) -> tuple[int, set[str]]:
    sql, figure, fact = _ground_select()
    clean = body
    for pat, flags in _STRIP_RE.findall(_BODY):
        clean = re.sub(pat, " ", clean, flags=re.M if "m" in flags else 0)
    con = _connect()
    con.executemany("INSERT INTO xtab VALUES (?, ?)",
                    [(i, m.group()) for i, m in enumerate(re.finditer(figure, clean, flags=re.I))])
    con.executemany("INSERT INTO ptab VALUES (?)", [(m.group(),) for m in re.finditer(fact, facts)])
    checked, n_bad, ungrounded = con.execute(sql).fetchone()
    bad = set(ungrounded.split(", ")) if ungrounded else set()
    assert n_bad == len(bad)
    return checked, bad


_CORPUS = [
    "Over the last 7 days the platform spent $12,345.67 (3,354.8 credits) across 1,234,567 queries.",
    "Spend was about $12.3K; 321 queries failed (0.03%). 3 of 900 task runs failed.",
    "There are 2 high alerts open and 0 critical; 5 alerts were raised in the last 24 hours.",
    "(1) Health is good. (2) Attention: 2 critical alerts. (3) Focus on WH_X1 and p95 latency.",
    "Spend was 12,345.67 credits this week.",
    "Spend was $3,354.80 this week.",
    "Queries: 1.2 million; 12.3 minutes queued; 4.56 GB spilled; 99.7% task success.",
    "On 2026-09-29 at 07:20 the digest ran; Q3 is on track; llama3.1-8b wrote this.",
    "1. Spend is stable.\n2. Nothing urgent.\n3. Keep watching.",
    "Spend rose 15% week over week.",
    "Average daily spend was $1,763.67.",
    "No numbers here at all.",
    "#1 priority: the 2 open high alerts.",
    "Failures: 321 failed queries, 3 failed tasks; 12m queued.",
    "about $12.3 thousand; 99.97 percent of queries succeeded; 1.1 million queries; $12.2K",
    "2 critical, 2 critical and 9 critical; 2 high; 5 days; 24 hours; 3 tasks failed.",
    "",
]


@pytest.mark.parametrize("body", _CORPUS)
def test_the_procs_own_matching_select_agrees_with_check_digest(body):
    checked, bad = _sql_check(body, FACTS)
    res = dg.check_digest(body, FACTS)
    assert (checked, bad) == (res.checked, set(res.ungrounded)), body


@pytest.mark.parametrize("facts, body", [
    ("FAILED_QUERY_PCT=1.25", "1.3% of queries failed"),
    ("FAILED_QUERY_PCT=1.25", "1.2% of queries failed"),
    ("FAILED_QUERY_PCT=0.75", "0.8% of queries failed"),
    ("TASK_FAILURE_PCT=0.15", "0.2% of tasks failed"),
    ("QUERIES=8250000", "8.3 million queries"),
    ("QUERIES=8250000000", "8.2 billion queries"),
])
def test_an_exact_half_step_rounding_is_grounded_in_the_proc_too(facts, body):
    """W3 (review r1): the proc's own join (IEEE doubles in sqlite, as in Snowflake FLOAT) and the mirror both
    accept a correctly rounded half-step figure."""
    assert _sql_check(body, facts) == (1, set())
    res = dg.check_digest(body, facts)
    assert (res.checked, res.ungrounded) == (1, ())


def test_the_half_step_slack_does_not_widen_the_procs_rule():
    assert _sql_check("1.4% of queries failed", "FAILED_QUERY_PCT=1.25") == (1, {"1.4%"})
    assert _sql_check("1.3% of queries failed", "FAILED_QUERY_PCT=1.2499") == (1, {"1.3%"})


def test_sqlite_parity_has_teeth():
    """The harness really runs the proc's join: a wrong fact value flips a matched figure to ungrounded, and an
    'n/a' fact licenses nothing -- in the SQL exactly as in Python."""
    assert _sql_check("There are 2 high alerts.", FACTS) == (1, set())
    assert _sql_check("There are 2 high alerts.", FACTS.replace("OPEN_HIGH_ALERTS=2", "OPEN_HIGH_ALERTS=7")) == \
        (1, {"2 high"})
    assert _sql_check("Spend was $12,345.67.", "SPEND_USD=n/a") == (1, {"$12,345.67"})
    assert dg.check_digest("Spend was $12,345.67.", "SPEND_USD=n/a").ungrounded == ("$12,345.67",)
