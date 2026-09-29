#!/usr/bin/env python3
"""Forward-generate V165: the morning digest is checked, not assumed (Next-Fifty #24, v4.602.0).

SP_DAILY_DIGEST (current definer V112, re-derived from V070) asks Cortex for a three-paragraph morning digest
from exec-board facts and alert counts, stores whatever comes back, and sends it to every digest route. Nothing
checks the numbers: the app labels it "grounded" regardless, the facts hand the model dollars under the key
CREDITS (CREDITS=<VALUE_USD>, since V007), a Cortex failure became the digest body and went to Teams, and the
text reached a Teams Workflows JSON template unescaped (raw CHR(10) and quotes: the V026 finding).

Owner decision 2026-09-29 (#24): store FACTS + a MEASURED GROUNDING_OK; when the AI draft's figures do not match
the facts, or Cortex fails, SEND A TEMPLATED DIGEST labelled not AI-written; fix the unescaped-newline JSON.

Reads V112__daily_digest_skips_paging_routes.sql ONLY (tests/test_proc_lineage.py: nothing after V112 re-defines
SP_DAILY_DIGEST) and emits, in order:

  guard (-20165, v < 164) -> 6 x ALTER TABLE DAILY_DIGEST ADD COLUMN IF NOT EXISTS -> marker + SP_DAILY_DIGEST
  re-derived from V112 -> SCHEMA_VERSION 165.

SP_DAILY_DIGEST deltas, each asserted by count (everything else byte-identical to V112; the V165 test reverses them):
  D1  DECLAREs after 'body VARCHAR;': typed facts, alert counts, grounding state, msg
  D2  the FACTS / ALERTS reads -> one conditional aggregation into typed variables + COUNT_IF alert counts; FACTS
      becomes named facts with one unit per key (SPEND_USD and CREDITS separate)
  D3  the prompt: every number must be a FACT value, no derived numbers, units in full, unnumbered paragraphs
  D4  Cortex failure: body NULL + ai_err (no error text as the digest); ai_body keeps the draft for audit
  D5  measured grounding (5 backslash-free strips, one SELECT over REGEXP_SUBSTR_ALL + FLATTEN) and the
      templated digest when GROUNDING_OK is NULL or FALSE
  D6  the INSERT adds FACTS, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, BODY_SOURCE, AI_BODY (same transaction)
  D7  digest_ai_failed ledger row; msg = header + body (+ AI footer), JSON-escaped with V064's five REPLACEs,
      RTRIM(LEFT(msg, 3000), CHR(92))
  D8  TEXT_PLAIN(:msg): the withheld draft is never sent
  D9  RETURN names the version written and why

The grounding literals are copies of app/logic/digest_grounding.py; the generator never imports app/ and the
permanent tests/test_digest_grounding_parity.py locks them against the LATEST proc body.

No SETTINGS key, no task change, no procedure run at apply time (a CALL would spend Cortex credits and post to
Teams). With PREFLIGHT_OUT set, also writes the read-only V165 section of PREFLIGHT_WAVE4.sql (P165.1-P165.3,
built from the SAME D2 / D3 / D5 text); with PART_B_OUT set, the read-only RUN_NEXT PART B grids V165.1-V165.3.

Run: python outputs/gen_v165.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V112__daily_digest_skips_paging_routes.sql"
V112 = BASE.read_text(encoding="utf-8")
NAME = "V165__daily_digest_grounding.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


# ---------------------------------------------------------------------------------------------------
# Literal copies of app/logic/digest_grounding.py (tests/test_digest_grounding_parity.py locks them).
# Backslash-free on purpose ([0-9], [.], [$]): one literal is valid in Python re and in a Snowflake
# string inside a $$ body (V022/V026).
# ---------------------------------------------------------------------------------------------------
STRIP_PATTERNS = (
    ("[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{1,2}:[0-9]{2}(:[0-9]{2})?)?", ""),
    ("[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?", ""),
    ("[A-Za-z_]+[0-9][A-Za-z0-9_]*", ""),
    ("[(][0-9]{1,2}[)]|#[0-9]{1,2}", ""),
    ("^[ *#]*[0-9]{1,2}[.)] ", "m"),
)
FIGURE_PATTERN = "[$]?([0-9]{1,3}(,[0-9]{3})+|[0-9]+)([.][0-9]+)?( ?[a-z]+)?( ?%)?"
NUM_PATTERN = "[0-9][0-9,]*([.][0-9]+)?"
WORD_PATTERN = "[A-Za-z]+"
FACT_PATTERN = "[A-Z][A-Z0-9_]*=[0-9]+([.][0-9]+)?"
KEYWORDS = (
    ("credit%", "CREDIT"), ("critical", "CRITICAL"), ("high", "HIGH"), ("minute%", "MINUTE"),
    ("gb", "_GB"), ("quer%", "QUER"), ("fail%", "FAIL"), ("task%", "TASK"), ("alert%", "ALERT"),
    ("hour%", "HOUR"), ("day%", "DAY"),
)
SCALE_WORDS = ((("k", "thousand"), 1000), (("m", "mm", "mn", "million"), 1000000),
               (("b", "bn", "billion"), 1000000000))
PCT_WORDS = ("percent", "pct")
REL_TOL = "0.005"
for _p, _f in STRIP_PATTERNS:
    assert "\\" not in _p and "'" not in _p and "$$" not in _p
for _p in (FIGURE_PATTERN, NUM_PATTERN, WORD_PATTERN, FACT_PATTERN):
    assert "\\" not in _p and "'" not in _p and "$$" not in _p


def _in(words: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{w}'" for w in words) + ")"


STRIP_LINES = "".join(
    f"        clean := REGEXP_REPLACE(:{'body' if i == 0 else 'clean'}, '{pat}', ' '"
    + (f", 1, 0, '{flags}'" if flags else "") + ");\n"
    for i, (pat, flags) in enumerate(STRIP_PATTERNS))

SCALE_CASE = ("CASE " + "\n                                ".join(
    f"WHEN w.WORD IN {_in(words)} THEN {scale}" for words, scale in SCALE_WORDS)
    + "\n                                ELSE 1 END AS SCALE")
KEYWORD_CASE = ("CASE " + "\n                                ".join(
    (f"WHEN w.WORD LIKE '{pat}' THEN '{key}'" if pat.endswith("%") else f"WHEN w.WORD = '{pat}' THEN '{key}'")
    for pat, key in KEYWORDS)
    + "\n                                ELSE NULL END AS KEYWORD")

# ---------------------------------------------------------------------------------------------------
# The measured-grounding SELECT (D5): one statement over the stripped draft and the FACTS string. The
# PREFLIGHT runs the same text per draft row (per_run below), so what the owner previews is what the proc runs.
# ---------------------------------------------------------------------------------------------------
GROUND_SELECT = f"""\
        SELECT COUNT(*), COUNT_IF(NOT g.MATCHED),
               NULLIF(LEFT(LISTAGG(IFF(g.MATCHED, NULL, g.TOK), ', ') WITHIN GROUP (ORDER BY g.POS), 1000), '')
          INTO :n_checked, :n_bad, :ungrounded
        FROM (
            SELECT t.TOK, MIN(t.POS) AS POS, COUNT(f.FVAL) > 0 AS MATCHED
            FROM (
                SELECT u.POS, u.TOK, u.UNIT, u.KEYWORD,
                       u.NUM_VAL * u.SCALE AS VAL,
                       GREATEST(0.5 * POWER(10, -u.DECIMALS) * u.SCALE, {REL_TOL} * u.NUM_VAL * u.SCALE) AS TOL
                FROM (
                    SELECT w.POS, w.TOK,
                           TRY_TO_DOUBLE(REPLACE(w.NUM, ',', '')) AS NUM_VAL,
                           IFF(CONTAINS(w.NUM, '.'), LENGTH(SPLIT_PART(w.NUM, '.', 2)), 0) AS DECIMALS,
                           CASE WHEN STARTSWITH(w.TOK, '$') THEN 'usd'
                                WHEN CONTAINS(w.TOK, '%') OR w.WORD IN {_in(PCT_WORDS)} THEN 'pct'
                                ELSE 'num' END AS UNIT,
                           {SCALE_CASE},
                           {KEYWORD_CASE}
                    FROM (
                        SELECT x.INDEX AS POS, TRIM(x.VALUE::VARCHAR) AS TOK,
                               REGEXP_SUBSTR(x.VALUE::VARCHAR, '{NUM_PATTERN}') AS NUM,
                               LOWER(REGEXP_SUBSTR(x.VALUE::VARCHAR, '{WORD_PATTERN}')) AS WORD
                        FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:clean,
                             '{FIGURE_PATTERN}', 1, 1, 'i'))) x
                    ) w
                ) u
            ) t
            LEFT JOIN (
                SELECT SPLIT_PART(p.VALUE::VARCHAR, '=', 1) AS FKEY,
                       TRY_TO_DOUBLE(SPLIT_PART(p.VALUE::VARCHAR, '=', 2)) AS FVAL
                FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:facts, '{FACT_PATTERN}'))) p
            ) f
              ON (t.UNIT = 'num' OR (t.UNIT = 'usd' AND ENDSWITH(f.FKEY, '_USD'))
                                 OR (t.UNIT = 'pct' AND ENDSWITH(f.FKEY, '_PCT')))
             AND (t.KEYWORD IS NULL OR CONTAINS(f.FKEY, t.KEYWORD))
             AND ABS(f.FVAL - t.VAL) <= t.TOL
            GROUP BY t.TOK
        ) g;
"""

# The FACTS string (D2): named facts, one unit per key. Right-hand sides shared with the PREFLIGHT.
FACTS_EXPR = """'WINDOW_DAYS=7; SPEND_USD=' || COALESCE(TO_VARCHAR(:f_spend_usd), 'n/a')
          || '; CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')
          || '; QUERIES=' || COALESCE(TO_VARCHAR(:f_queries), 'n/a')
          || '; FAILED_QUERIES=' || COALESCE(TO_VARCHAR(:f_failed_q), 'n/a')
          || '; FAILED_QUERY_PCT=' || COALESCE(TO_VARCHAR(:f_failed_q_pct), 'n/a')
          || '; QUERY_SUCCESS_PCT=' || COALESCE(TO_VARCHAR(100 - :f_failed_q_pct), 'n/a')
          || '; QUEUED_MINUTES=' || COALESCE(TO_VARCHAR(:f_queued_min), 'n/a')
          || '; SPILL_GB=' || COALESCE(TO_VARCHAR(:f_spill_gb), 'n/a')
          || '; TASK_RUNS=' || COALESCE(TO_VARCHAR(:f_task_runs), 'n/a')
          || '; TASK_FAILURES=' || COALESCE(TO_VARCHAR(:f_task_fail), 'n/a')
          || '; TASK_FAILURE_PCT=' || COALESCE(TO_VARCHAR(:f_task_fail_pct), 'n/a')
          || '; TASK_SUCCESS_PCT=' || COALESCE(TO_VARCHAR(100 - :f_task_fail_pct), 'n/a')"""
ALERTS_EXPR = """'ALERT_WINDOW_HOURS=24; OPEN_CRITICAL_ALERTS=' || :a_open_crit
           || '; OPEN_HIGH_ALERTS=' || :a_open_high || '; ALERTS_RAISED_24H=' || :a_raised_24h"""
KPI_SELECT = """SELECT MAX(IFF(METRIC = 'CREDITS', VALUE_USD, NULL)), MAX(IFF(METRIC = 'CREDITS', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUERIES', VALUE, NULL)), MAX(IFF(METRIC = 'FAILED_QUERIES', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUEUED_MINUTES', VALUE, NULL)), MAX(IFF(METRIC = 'SPILL_GB', VALUE, NULL)),
           MAX(IFF(METRIC = 'TASK_RUNS', VALUE, NULL)), MAX(IFF(METRIC = 'TASK_FAILURES', VALUE, NULL))"""
KPI_FROM = """    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';"""
ALERT_SELECT = """SELECT COUNT_IF(SEVERITY = 'CRITICAL' AND STATUS IN ('OPEN','ACK')),
           COUNT_IF(SEVERITY = 'HIGH' AND STATUS IN ('OPEN','ACK')),
           COUNT_IF(RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()))"""
# (variable, declared type): the D1 typed facts, in the D2 INTO order, then the two derived rates
FACT_VARS = (("f_spend_usd", "NUMBER(38,2)"), ("f_credits", "NUMBER(38,2)"), ("f_queries", "NUMBER(38,0)"),
             ("f_failed_q", "NUMBER(38,0)"), ("f_queued_min", "NUMBER(38,1)"), ("f_spill_gb", "NUMBER(38,2)"),
             ("f_task_runs", "NUMBER(38,0)"), ("f_task_fail", "NUMBER(38,0)"))
RATE_VARS = (("f_failed_q_pct", "NUMBER(38,2)", "ROUND(100 * :f_failed_q / NULLIF(:f_queries, 0), 2)"),
             ("f_task_fail_pct", "NUMBER(38,2)", "ROUND(100 * :f_task_fail / NULLIF(:f_task_runs, 0), 2)"))
ALERT_VARS = ("a_open_crit", "a_open_high", "a_raised_24h")

# The prompt (D3). The shared right-hand side; :facts is the only bind.
PROMPT_EXPR = """LEFT(
        'You are a senior Snowflake DBA writing the morning digest for ALFA/Trexis leadership. '
        || 'Use ONLY the FACTS below (the last 7 days, all companies; alert counts are open now or raised in the last 24 hours). '
        || 'Every number you write must be a FACT value, copied or rounded (thousands separators are fine): never calculate '
        || 'totals, averages, differences or new percentages, and never write dates or times. Dollar amounts come only from '
        || '*_USD facts and percentages only from *_PCT facts; CREDITS are Snowflake credits, not dollars. Write units in full '
        || '(credits, minutes, GB). Write three short unnumbered paragraphs: platform health and spend in plain language; '
        || 'what needs attention today and why; one recommended focus. No preamble. '
        || 'FACTS: ' || COALESCE(:facts, 'none') || '.',
        6000)"""

# The templated digest (D5), built ONLY from the fact variables. Its only digit literals are 7 and 24 (the
# TO_VARCHAR format models aside); the reason names Cortex (NULL) or the mismatch (FALSE).
TEMPLATE_EXPR = """'Templated digest (not AI-written): '
             || IFF(:grounding_ok IS NULL,
                    'Cortex returned no digest this morning, so OVERWATCH sent the exec-board facts directly.',
                    'the AI draft stated figures that do not match the exec-board facts, so OVERWATCH sent the facts directly.')
             || CHR(10) || CHR(10)
             || 'Last 7 days, all companies: spend ' || COALESCE('$' || TRIM(TO_VARCHAR(:f_spend_usd, '999,999,999,990.00')), 'n/a')
             || ' (' || COALESCE(TRIM(TO_VARCHAR(:f_credits, '999,999,999,990.00')), 'n/a') || ' credits); '
             || COALESCE(TRIM(TO_VARCHAR(:f_queries, '999,999,999,990')), 'n/a') || ' queries, '
             || COALESCE(TRIM(TO_VARCHAR(:f_failed_q, '999,999,999,990')), 'n/a') || ' failed ('
             || COALESCE(TO_VARCHAR(:f_failed_q_pct), 'n/a') || '%); '
             || COALESCE(TRIM(TO_VARCHAR(:f_queued_min, '999,999,999,990.0')), 'n/a') || ' minutes queued; '
             || COALESCE(TRIM(TO_VARCHAR(:f_spill_gb, '999,999,999,990.00')), 'n/a') || ' GB spilled to remote storage; '
             || COALESCE(TRIM(TO_VARCHAR(:f_task_runs, '999,999,999,990')), 'n/a') || ' task runs, '
             || COALESCE(TRIM(TO_VARCHAR(:f_task_fail, '999,999,999,990')), 'n/a') || ' failed ('
             || COALESCE(TO_VARCHAR(:f_task_fail_pct), 'n/a') || '%).'
             || CHR(10) || CHR(10)
             || 'Alerts: ' || :a_open_crit || ' critical and ' || :a_open_high || ' high open; '
             || :a_raised_24h || ' raised in the last 24 hours.'
             || CHR(10) || CHR(10)
             || 'Focus: ' || CASE WHEN :a_open_crit > 0 THEN 'clear the ' || :a_open_crit || ' open critical alert(s) first.'
                                  WHEN :a_open_high > 0 THEN 'work the ' || :a_open_high || ' open high alert(s).'
                                  WHEN COALESCE(:f_task_fail, 0) > 0 THEN 'review the ' || :f_task_fail || ' failed task run(s).'
                                  ELSE 'nothing is open at critical or high; no action is needed today.' END"""

# V064:203-207, verbatim (the notifier's v3 JSON escape), re-targeted from message to msg.
ESCAPE_LINES = """\
    msg := REPLACE(:msg, CHR(92), CHR(92) || CHR(92));
    msg := REPLACE(:msg, CHR(34), CHR(92) || CHR(34));
    msg := REPLACE(:msg, CHR(10), CHR(92) || 'n');
    msg := REPLACE(:msg, CHR(13), '');
    msg := REPLACE(:msg, CHR(9),  CHR(92) || 't');
"""

# ---------------------------------------------------------------------------------------------------
# The delta blocks.
# ---------------------------------------------------------------------------------------------------
D1_BLOCK = (
    "    -- D1 >>> V165 #24: the facts as numbers, the measured grounding and the version sent\n"
    + "".join(f"    {v} {t};\n" for v, t in FACT_VARS)
    + "".join(f"    {v} {t};\n" for v, t, _ in RATE_VARS)
    + "".join(f"    {v} NUMBER(38,0);\n" for v in ALERT_VARS)
    + """\
    ai_err VARCHAR;
    ai_body VARCHAR;
    clean VARCHAR;
    n_checked INT;
    n_bad INT DEFAULT 0;
    ungrounded VARCHAR;
    grounding_ok BOOLEAN;
    body_source VARCHAR DEFAULT 'AI';
    msg VARCHAR;
    -- <<< D1
""")

D2_BLOCK = f"""\
    -- D2 >>> V165 #24: FACTS are named numbers with one unit per key (*_USD dollars, *_PCT percent, the rest
    -- counts, credits, minutes or GB), so the draft's figures can be checked against them. V007-V112 sent
    -- 'CREDITS=<dollars>' (COALESCE(VALUE_USD, VALUE) under the CREDITS metric); spend now has its own key.
    {KPI_SELECT}
      INTO {', '.join(':' + v for v, _ in FACT_VARS)}
{KPI_FROM}

    -- COUNT_IF, not V112's SUM(IFF(...)): an empty ALERT_EVENTS reads 0, never a NULL that blanks the string
    {ALERT_SELECT}
      INTO {', '.join(':' + v for v in ALERT_VARS)}
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;

{''.join(f'    {v} := {expr};{chr(10)}' for v, _, expr in RATE_VARS)}
    facts := {FACTS_EXPR};
    alerts := {ALERTS_EXPR};
    facts := :facts || '; ' || :alerts;
    -- <<< D2
"""

D3_BLOCK = f"""\
    -- D3 >>> V165 #24: the prompt forbids derived numbers (the check would reject them) and names the units
    prompt := {PROMPT_EXPR};
    -- <<< D3
"""

D4_HANDLER = """\
            -- D4: V165 #24 - no error text as the digest; the template goes out and the failure is ledgered below
            body := NULL;
            ai_err := SQLERRM;
"""
D4_AI_BODY = "    ai_body := LEFT(:body, 8000);   -- D4: the draft, kept for audit whichever version is sent\n"

D5_BLOCK = f"""\
    -- D5 >>> V165 #24: measured grounding. Every figure the draft states must equal a FACT within half a step
    -- of its shown precision or 0.5% (app/logic/ai_grounding.check_grounding's tolerance): a $ figure only a
    -- *_USD fact, a % figure only a *_PCT fact, a figure followed by a known noun (credits, critical, high,
    -- minutes, GB, queries, failed, tasks, alerts, hours, days) only a fact whose key names it, any other
    -- figure any fact. Dates, clock times, identifier-like tokens (WH_X1, p95, V112) and list markers are
    -- stripped first. app/logic/digest_grounding.py mirrors this rule; tests/test_digest_grounding_parity.py
    -- locks every literal below to it. Backslash-free patterns on purpose ([0-9], [.], [$]): V022/V026.
    IF (:body IS NOT NULL AND TRIM(:body) <> '') THEN
{STRIP_LINES}{GROUND_SELECT}        grounding_ok := (n_bad = 0);
    END IF;

    -- V165 #24: the templated digest, built ONLY from the fact variables above and labelled not AI-written.
    -- Sent when the draft states a figure no fact supports (GROUNDING_OK = FALSE) or Cortex returned nothing
    -- (GROUNDING_OK NULL). The withheld draft stays in AI_BODY for audit and is never sent.
    IF (grounding_ok IS NULL OR NOT grounding_ok) THEN
        body_source := 'TEMPLATE';
        body := {TEMPLATE_EXPR};
    END IF;
    -- <<< D5
"""

D6_INSERT = """\
        -- D6: V165 #24 - the facts, the measured result and both texts
        INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST
            (DIGEST_DATE, COMPANY, MODEL, BODY, FACTS, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, BODY_SOURCE, AI_BODY)
        VALUES (CURRENT_DATE(), 'ALL', :model, LEFT(:body, 8000), LEFT(:facts, 4000), :grounding_ok,
                :n_checked, :ungrounded, :body_source, :ai_body);
"""

D7_BLOCK = f"""\
    -- D7 >>> V165 #24: a Cortex failure is ledgered (it used to become the digest body and go to Teams)
    IF (ai_err IS NOT NULL) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'DailyDigest', 'digest_ai_failed', :ai_err,
               'Cortex COMPLETE failed for model ' || :model || ' - the templated digest was written and sent instead',
               CURRENT_ROLE();
    END IF;

    -- V165 #24: the SENT text is the chosen BODY (never the withheld draft), JSON-escaped exactly like
    -- SP_NOTIFY_WEBHOOK (V026 v3, V064): every route's body template splices the message INSIDE a JSON
    -- string, and V070/V112 sent a raw CHR(10) plus raw Cortex prose, which Teams Workflows rejects.
    msg := 'OVERWATCH morning digest — ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) || LEFT(:body, 3000)
        || IFF(:body_source = 'AI',
               CHR(10) || CHR(10) || 'AI-written (' || :model || '); '
               || IFF(:n_checked = 0, 'it states no figures.',
                      'all ' || :n_checked || ' of its figures match the exec-board facts.'),
               '');
{ESCAPE_LINES}    msg := RTRIM(LEFT(:msg, 3000), CHR(92));   -- a cut escape pair must not escape the template's closing quote
    -- <<< D7
"""

D8_SEND = "                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(:msg),   -- D8: V165 #24\n"

D9_RETURN = """\
    -- D9: V165 #24 - the return names the version written and why
    RETURN 'digest written (' || :body_source
           || IFF(:grounding_ok = FALSE, '; the AI draft had ' || :n_bad || ' unmatched figure(s)', '')
           || IFF(:ai_err IS NOT NULL, '; Cortex failed', '')
           || '); sent ' || :routes_sent || '/' || :routes_total || ' routes'
           || IFF(:routes_total > 0 AND :routes_sent = 0, ' [UNDELIVERED]', '');
"""

# ---------------------------------------------------------------------------------------------------
# V112 anchors (the old side of each delta).
# ---------------------------------------------------------------------------------------------------
OLD_D2 = """\
    SELECT COALESCE(LISTAGG(METRIC || '=' || COALESCE(VALUE_USD, VALUE)::VARCHAR, '; ')
           WITHIN GROUP (ORDER BY SORT_ORDER), 'no board rows')
      INTO :facts
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';

    SELECT 'open_critical=' || SUM(IFF(SEVERITY = 'CRITICAL' AND STATUS IN ('OPEN','ACK'), 1, 0))
           || '; open_high=' || SUM(IFF(SEVERITY = 'HIGH' AND STATUS IN ('OPEN','ACK'), 1, 0))
           || '; raised_24h=' || SUM(IFF(RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()), 1, 0))
      INTO :alerts
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;
"""
OLD_D3 = """\
    prompt := LEFT(
        'You are a senior Snowflake DBA writing the morning digest for ALFA/Trexis leadership. '
        || 'Use ONLY these 7-day platform facts and alert counts - never invent numbers. '
        || 'Write 3 short paragraphs: (1) platform health and spend in plain language, '
        || '(2) what needs attention today and why, (3) one recommended focus. No preamble. '
        || 'FACTS: ' || COALESCE(:facts, 'none') || '. ALERTS: ' || COALESCE(:alerts, 'none') || '.',
        6000);
"""
OLD_D4 = """\
            body := 'Digest unavailable: Cortex COMPLETE failed for model ' || :model
                    || '. Check SNOWFLAKE.CORTEX_USER grant and regional model availability.';
"""
ANCHOR_D45 = "    END;\n\n    -- V070 #39: replace today's digest atomically."
OLD_D6 = """\
        INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST (DIGEST_DATE, COMPANY, MODEL, BODY)
        VALUES (CURRENT_DATE(), 'ALL', :model, LEFT(:body, 8000));
"""
ANCHOR_D7 = "    END;\n\n\n    -- V070 #23: deliver the digest through EVERY enabled ALERT_ROUTES row's own"
OLD_D8 = """\
                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                    'OVERWATCH morning digest — ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) ||
                    LEFT(:body, 3000)),
"""
OLD_D9 = """\
    RETURN 'digest written; sent ' || :routes_sent || '/' || :routes_total || ' routes'
           || IFF(:routes_total > 0 AND :routes_sent = 0, ' [UNDELIVERED]', '');
"""

proc = extract_proc(V112, "SP_DAILY_DIGEST()")
assert proc.count("CHR(10)") == 1 and "\\" not in proc
proc = _swap(proc, "    body VARCHAR;\n", "    body VARCHAR;\n" + D1_BLOCK, "D1")
proc = _swap(proc, OLD_D2, D2_BLOCK, "D2")
proc = _swap(proc, OLD_D3, D3_BLOCK, "D3")
proc = _swap(proc, OLD_D4, D4_HANDLER, "D4 handler")
proc = _swap(proc, ANCHOR_D45, "    END;\n" + D4_AI_BODY + "\n" + D5_BLOCK + "\n    -- V070 #39: replace today's digest atomically.",
             "D4 ai_body + D5")
proc = _swap(proc, OLD_D6, D6_INSERT, "D6")
proc = _swap(proc, ANCHOR_D7, "    END;\n\n" + D7_BLOCK + "\n    -- V070 #23: deliver the digest through EVERY enabled ALERT_ROUTES row's own", "D7")
proc = _swap(proc, OLD_D8, D8_SEND, "D8")
proc = _swap(proc, OLD_D9, D9_RETURN, "D9")

_body = proc[proc.index("$$") + 2:proc.rindex("$$")]
assert "\\" not in _body and "$$" not in _body, "backslash-free body, no inner $$"
assert "Digest unavailable" not in proc and proc.count("TEXT_PLAIN(:msg)") == 1
assert proc.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST") == 1
assert proc.count("UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'") == 1        # V112 carry-over

# ---------------------------------------------------------------------------------------------------
# The migration file.
# ---------------------------------------------------------------------------------------------------
COLUMNS = (("FACTS", "VARCHAR(4000)"), ("GROUNDING_OK", "BOOLEAN"), ("FIGURES_CHECKED", "NUMBER(6,0)"),
           ("UNGROUNDED", "VARCHAR(1000)"), ("BODY_SOURCE", "VARCHAR(20)"), ("AI_BODY", "VARCHAR(8000)"))

HEADER = f"""-- {NAME}
--
-- Next-Fifty #24: the morning digest is checked, not assumed.
--
-- WHY: SP_DAILY_DIGEST (V112, re-derived from V070) sends a Cortex-written three-paragraph digest to every digest
-- route each morning and the app labels it "grounded", but nothing ever checked its numbers. The facts handed
-- the model dollars under the key CREDITS (COALESCE(VALUE_USD, VALUE) since V007), a Cortex failure became the
-- digest body and went to Teams, and the text reached the Teams Workflows JSON body template unescaped (a raw
-- CHR(10) plus raw prose: V026 recorded that Teams rejects that card, and the send is asynchronous, so
-- 'sent 1/1' can hide it). Owner decision 2026-09-29: store the facts and a MEASURED grounding result; when the
-- AI draft's figures do not match the facts, or Cortex fails, send a templated digest labelled not AI-written.
--
--   + DAILY_DIGEST columns (ADD COLUMN IF NOT EXISTS, all nullable): FACTS (the named facts given to the model),
--     GROUNDING_OK (TRUE = every figure matched, FALSE = at least one did not, NULL = not measured: Cortex
--     returned nothing, or a row written before V165), FIGURES_CHECKED, UNGROUNDED (the unmatched figures),
--     BODY_SOURCE ('AI' or 'TEMPLATE': which version BODY holds and the routes received) and AI_BODY (the
--     Cortex draft, kept for audit; a withheld draft is never sent).
--   ~ SP_DAILY_DIGEST re-derived from V112 (its current definer), byte-identical except:
--       D1 typed fact / grounding DECLAREs; D2 one conditional aggregation over the same MART_EXEC_BOARD KPI rows
--       into typed variables, COUNT_IF alert counts (an empty ALERT_EVENTS reads 0, not NULL), FACTS as named
--       facts with one unit per key (SPEND_USD and CREDITS separate); D3 a prompt that allows only FACT values,
--       copied or rounded, units in full, unnumbered paragraphs; D4 a Cortex failure leaves the body empty and is
--       ledgered as digest_ai_failed (no error text as the digest); D5 the measured grounding (each figure must
--       equal a fact within half a step of its shown precision or 0.5%, bound by unit -- $ only *_USD, % only
--       *_PCT -- and by the noun after it) and, when GROUNDING_OK is NULL or FALSE, a template built only from the
--       facts; D6 the INSERT stores the six new columns inside the unchanged V070 transaction; D7 the sent text is
--       JSON-escaped with SP_NOTIFY_WEBHOOK's five REPLACEs (V064) and cut back past a trailing backslash; D8 the
--       send uses that text; D9 the RETURN names the version written and why. Kept byte-identical: V112's
--       CRITICAL-only route filter, V070's DELIVER_DIGEST gate, the atomic DELETE + INSERT, digest_send_failed
--       and digest_undelivered.
--
-- COST: the same one Cortex call a day; the check adds a few small SQL statements to the 07:20 task (no
-- ACCOUNT_USAGE read, no new task or warehouse resume).
-- LATENCY: unchanged; TASK_DAILY_DIGEST runs 07:20 America/Chicago.
-- FIRST RUN: the next 07:20 run writes the first measured row. Nothing runs at apply time (a CALL would spend
-- Cortex credits and post to Teams). Preview with PREFLIGHT_WAVE4.sql P165.1-P165.3 (read-only).
-- ROLLBACK: re-run V112's SP_DAILY_DIGEST (V112__daily_digest_skips_paging_routes.sql, the CREATE PROCEDURE);
-- the columns can stay (new rows then carry NULLs, which the app shows as grounding not measured).
-- Apply AFTER V164. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20165, 'V165 requires V164 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 164) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The measured-digest columns (all nullable; the table is TRANSIENT, V007). Before the proc: an apply that
-- stops here leaves V112's proc writing its four named columns, which still works.
""" + "".join(f"ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS {c} {t};\n"
              for c, t in COLUMNS) + "\n"

MARKER = ("-- >>> derived:SP_DAILY_DIGEST  (from V112; FACTS + measured GROUNDING_OK + templated digest on mismatch + "
          "JSON-safe send, V165)\n")

DESCRIPTION = (
    "Morning digest grounding (Next-Fifty #24): DAILY_DIGEST + FACTS, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, "
    "BODY_SOURCE, AI_BODY (nullable). SP_DAILY_DIGEST re-derived from V112, byte-identical except: FACTS are named "
    "facts with one unit per key (SPEND_USD and CREDITS separate; V007-V112 sent dollars under CREDITS), COUNT_IF "
    "alert counts; a prompt that allows only FACT values; every figure in the Cortex draft is checked against the "
    "facts (unit and noun bound, within half a step of its precision or 0.5 percent); when a figure does not match, "
    "or Cortex fails (digest_ai_failed, no error text as the digest), a templated digest built only from the facts "
    "and labelled not AI-written is written and sent, the draft kept in AI_BODY; the sent text is JSON-escaped like "
    "SP_NOTIFY_WEBHOOK (V064) so a Teams Workflows card is valid. Kept: the CRITICAL-only route filter, the "
    "DELIVER_DIGEST gate, the atomic write, the per-route ledger. No task change, no SETTINGS key, no procedure run "
    "at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 165 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 165);
"""

out = HEADER + MARKER + proc + "\n" + VERSION_ROW

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n")
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("$$") == 4
assert out.count("\nALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS ") == 6
assert "V165 requires V164 first" in out and "SELECT 165 AS VERSION" in out
assert "\r" not in out
assert not re.search(r"^\s*CALL\s", out.replace(
    "            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(", ""), re.M), "no CALL but the route send"
assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+TASK|ALTER\s+TASK|EXECUTE\s+TASK)", out)
assert "@" not in out, "no address of any kind"

target = Path(os.environ.get("V165_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------------------------------
# PREFLIGHT (read-only; the V165 section of PREFLIGHT_WAVE4.sql). Built from the SAME text as the proc:
# the D2 KPI / alert reads, FACTS_EXPR, TEMPLATE_EXPR, PROMPT_EXPR, the strips and the grounding SELECT,
# with the scripting binds swapped for CTE columns.
# ---------------------------------------------------------------------------------------------------
_BIND = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_STR = re.compile(r"'(?:[^']|'')*'")


def _rebind(sql: str, mapping: dict[str, str]) -> str:
    """Every :bind outside string literals -> its mapped SQL; an unmapped bind fails closed."""
    parts, last = [], 0
    for m in _STR.finditer(sql):
        parts.append(_BIND.sub(lambda b: mapping[b.group(1)], sql[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_BIND.sub(lambda b: mapping[b.group(1)], sql[last:]))
    return "".join(parts)


def _strip_expr(src: str) -> str:
    """The five D5 strips as one nested expression (the proc runs them as five assignments)."""
    expr = src
    for pat, flags in STRIP_PATTERNS:
        expr = f"REGEXP_REPLACE({expr}, '{pat}', ' '" + (f", 1, 0, '{flags}'" if flags else "") + ")"
    return expr


def per_run_grounding() -> str:
    """GROUND_SELECT run once per row of a ``drafts`` CTE (RUN_ID, CLEAN) against ``k.FACTS``."""
    g = GROUND_SELECT
    g = _swap(g, "        SELECT COUNT(*), COUNT_IF(NOT g.MATCHED),\n",
              "        SELECT g.RUN_ID, COUNT(*) AS FIGURES_CHECKED, COUNT_IF(NOT g.MATCHED) AS N_BAD,\n", "P1")
    g = _swap(g, "WITHIN GROUP (ORDER BY g.POS), 1000), '')\n          INTO :n_checked, :n_bad, :ungrounded\n",
              "WITHIN GROUP (ORDER BY g.POS), 1000), '') AS UNGROUNDED\n", "P2")
    g = _swap(g, "SELECT t.TOK, MIN(t.POS)", "SELECT t.RUN_ID, t.TOK, MIN(t.POS)", "P3")
    g = _swap(g, "SELECT u.POS, u.TOK,", "SELECT u.RUN_ID, u.POS, u.TOK,", "P4")
    g = _swap(g, "SELECT w.POS, w.TOK,", "SELECT w.RUN_ID, w.POS, w.TOK,", "P5")
    g = _swap(g, "SELECT x.INDEX AS POS,", "SELECT d.RUN_ID, x.INDEX AS POS,", "P6")
    g = _swap(g, "FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:clean,",
              "FROM drafts d, TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(d.CLEAN,", "P7")
    g = _swap(g, "FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:facts,",
              "FROM k, TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(k.FACTS,", "P8")
    g = _swap(g, "            GROUP BY t.TOK\n        ) g;\n",
              "            GROUP BY t.RUN_ID, t.TOK\n        ) g\n        GROUP BY g.RUN_ID", "P9")
    assert ":" + "clean" not in g and ":facts" not in g and "INTO" not in g
    return g


def _cols(alias: str) -> dict[str, str]:
    """Every D1 fact / alert variable -> <alias>.<COLUMN> (the PREFLIGHT's CTE columns)."""
    names = [v for v, _ in FACT_VARS] + [v for v, _, _ in RATE_VARS] + list(ALERT_VARS)
    return {v: f"{alias}.{v.upper()}" for v in names}


_K_MAP = _cols("k")


def _facts_ctes() -> str:
    kpi_cols = re.findall(r"MAX\(IFF\(METRIC = '\w+', VALUE(?:_USD)?, NULL\)\)", KPI_SELECT)
    assert len(kpi_cols) == len(FACT_VARS) and ", ".join(kpi_cols) == " ".join(KPI_SELECT[7:].split())
    kpi = ",\n               ".join(f"({c})::{t} AS {v.upper()}" for c, (v, t) in zip(kpi_cols, FACT_VARS, strict=True))
    al_cols = [c.strip() for c in ALERT_SELECT[len("SELECT "):].split(",\n")]
    assert len(al_cols) == len(ALERT_VARS)
    al = ",\n               ".join(f"{c} AS {v.upper()}" for c, v in zip(al_cols, ALERT_VARS, strict=True))
    rates = ",\n               ".join(f"({_rebind(expr, _cols('b'))})::{t} AS {v.upper()}" for v, t, expr in RATE_VARS)
    facts = _rebind(FACTS_EXPR, _cols("kr")) + "\n          || '; ' || " + _rebind(ALERTS_EXPR, _cols("kr"))
    return f"""kpi AS (
        SELECT {kpi}
    {KPI_FROM.strip().rstrip(';')}
    ),
    al AS (
        SELECT {al}
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    ),
    b AS (SELECT kpi.*, al.* FROM kpi CROSS JOIN al),
    kr AS (
        SELECT b.*,
               {rates}
        FROM b
    ),
    k AS (
        SELECT kr.*,
               {facts} AS FACTS
        FROM kr
    )"""


def _tpl(grounding_ok: str) -> str:
    return _rebind(TEMPLATE_EXPR, {**_K_MAP, "grounding_ok": grounding_ok})


PREFLIGHT = f"""-- PREFLIGHT_WAVE4 -- V165 section (READ-ONLY). Run before applying V165; changes nothing.
-- What the grids answer: P165.1a-e today's digest state (what V165 replaces); P165.2 V165's FACTS and templated
-- digest rendered from the live exec board, with V165's figure check (REGEXP_SUBSTR_ALL + FLATTEN, their first use
-- in this repo) run on the live engine; P165.3 (OPTIONAL, about 5 small Cortex calls) the new prompt drafted 5
-- times, each draft checked exactly as V165 will.

-- P165.1a the last 14 digests. expect one row a day; UNAVAILABLE TRUE = a Cortex failure that V112 sent as the body.
SELECT DIGEST_DATE, MODEL, LENGTH(BODY) AS BODY_LEN, STARTSWITH(BODY, 'Digest unavailable') AS UNAVAILABLE,
       LEFT(BODY, 300) AS BODY_START, CREATED_AT
  FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST
 ORDER BY DIGEST_DATE DESC
 LIMIT 14;

-- P165.1b the digest task, 14 days (ACCOUNT_USAGE, up to ~45 min behind). expect SUCCEEDED; RETURN_VALUE is NULL (a
--         task that CALLs a proc does not publish the proc's return string -- the V160.6 finding).
SELECT SCHEDULED_TIME, STATE, RETURN_VALUE, ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH' AND NAME = 'TASK_DAILY_DIGEST'
   AND SCHEDULED_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
 ORDER BY SCHEDULED_TIME DESC;

-- P165.1c digest ledger rows, 30 days, by type. expect none, or digest_send_failed / digest_undelivered if a route failed.
SELECT ERROR_TYPE, COUNT(*) AS N, MAX(LOGGED_AT) AS LAST_AT, MAX(LEFT(ERROR_MESSAGE, 200)) AS SAMPLE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE PAGE = 'DailyDigest' AND LOGGED_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP())
 GROUP BY ERROR_TYPE
 ORDER BY N DESC;

-- P165.1d the digest-eligible routes (the V112 cursor). expect the HIGH Teams route.
SELECT r.ROUTE_ID, r.FAMILY, r.MIN_SEVERITY, r.INTEGRATION_NAME, r.DELIVER_DIGEST
  FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
 WHERE r.ENABLED AND r.DELIVER_DIGEST AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'
 ORDER BY r.ROUTE_ID;

-- P165.2 V165's FACTS and templated digest, rendered from the live exec board, and V165's figure check run on it
--        (RUN 1: the template, built only from the facts, expect GROUNDING_OK TRUE and FIGURES_CHECKED > 0) and on a
--        negative control (RUN 2: a figure no fact can match, expect GROUNDING_OK FALSE and UNGROUNDED '999999 critical').
--        TEMPLATED_BODY shows the mismatch wording; a Cortex failure changes only its first sentence.
--        COUNT_IF_ON_EMPTY expect 0: the proc counts a draft with no figures as grounded on that.
WITH {_facts_ctes()},
    tpl AS (
        SELECT {_tpl("FALSE")} AS BODY
        FROM k
    ),
    drafts AS (
        SELECT 1 AS RUN_ID, 'template' AS DRAFT_KIND, t.BODY, {_strip_expr("t.BODY")} AS CLEAN FROM tpl t
        UNION ALL
        SELECT 2, 'negative control', c.BODY, {_strip_expr("c.BODY")}
        FROM (SELECT 'There are 999999 critical alerts open.' AS BODY) c
    ),
    gr AS (
{per_run_grounding()}
    )
SELECT d.RUN_ID, d.DRAFT_KIND, COALESCE(gr.FIGURES_CHECKED, 0) AS FIGURES_CHECKED, COALESCE(gr.N_BAD, 0) AS N_BAD,
       gr.UNGROUNDED, COALESCE(gr.N_BAD, 0) = 0 AS GROUNDING_OK, k.FACTS, d.BODY AS TEMPLATED_BODY,
       (SELECT COUNT_IF(TRUE) FROM drafts z WHERE FALSE) AS COUNT_IF_ON_EMPTY
  FROM drafts d
  CROSS JOIN k
  LEFT JOIN gr ON gr.RUN_ID = d.RUN_ID
 ORDER BY d.RUN_ID;

-- P165.1e digest sends, 13 days (NOTIFICATION_HISTORY takes START_TIME and at most 336h), around the 07:20 CT run.
--         expect SUCCESS rows; none at all, or FAILED ones, confirm the unescaped-JSON card defect V165 fixes. Runs
--         after P165.2 so an unreadable function stops nothing else.
SELECT CONVERT_TIMEZONE('America/Chicago', CREATED)::TIMESTAMP_NTZ AS CREATED_CT, INTEGRATION_NAME, STATUS,
       LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(
           START_TIME => DATEADD('day', -13, CURRENT_TIMESTAMP()), RESULT_LIMIT => 10000))
 WHERE INTEGRATION_NAME IN (SELECT r.INTEGRATION_NAME FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
                             WHERE r.ENABLED AND r.DELIVER_DIGEST AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL')
   AND HOUR(CONVERT_TIMEZONE('America/Chicago', CREATED)) = 7
   AND MINUTE(CONVERT_TIMEZONE('America/Chicago', CREATED)) BETWEEN 15 AND 45
 ORDER BY CREATED DESC;

-- P165.3 OPTIONAL (about 5 small Cortex calls on CORTEX_MODEL; skip it by stopping here). The new prompt, drafted 5
--        times, each draft checked exactly as V165 will. WOULD_SEND 'TEMPLATE' is the expected template rate.
WITH {_facts_ctes()},
    m AS (
        SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b') AS MODEL
        FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
    ),
    raw AS (
        SELECT SEQ4() + 1 AS RUN_ID,
               SNOWFLAKE.CORTEX.COMPLETE(m.MODEL, {_rebind(PROMPT_EXPR, {"facts": "k.FACTS"})}) AS BODY
        FROM TABLE(GENERATOR(ROWCOUNT => 5)) CROSS JOIN m CROSS JOIN k
    ),
    drafts AS (
        SELECT r.RUN_ID, r.BODY, {_strip_expr("r.BODY")} AS CLEAN FROM raw r
    ),
    gr AS (
{per_run_grounding()}
    )
SELECT d.RUN_ID, COALESCE(gr.FIGURES_CHECKED, 0) AS FIGURES_CHECKED, gr.UNGROUNDED,
       IFF(d.BODY IS NOT NULL AND TRIM(d.BODY) <> '' AND COALESCE(gr.N_BAD, 0) = 0, 'AI', 'TEMPLATE') AS WOULD_SEND,
       d.BODY AS AI_DRAFT
  FROM drafts d
  LEFT JOIN gr ON gr.RUN_ID = d.RUN_ID
 ORDER BY d.RUN_ID;
"""

# ---------------------------------------------------------------------------------------------------
# RUN_NEXT PART B (read-only verify grids). Every GET_DDL fragment is quote-free and in the proc body.
# ---------------------------------------------------------------------------------------------------
PART_B_PRESENT = ("Templated digest (not AI-written)", "GROUNDING_OK", "digest_ai_failed", "TEXT_PLAIN(:msg)",
                  "REGEXP_SUBSTR_ALL", "SPEND_USD=", "RTRIM(LEFT(:msg, 3000), CHR(92))",
                  "UPPER(COALESCE(r.MIN_SEVERITY, ", "digest written (")
PART_B_ABSENT = ("Digest unavailable", "open_critical=", "digest written; sent")
for _frag in PART_B_PRESENT + PART_B_ABSENT:
    assert not set(_frag) & {"'", "\\", "\n", "\r"}, _frag
for _frag in PART_B_PRESENT:
    assert _frag in _body, _frag
for _frag in PART_B_ABSENT:
    assert _frag not in _body, _frag

_DDL = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()')"
_ddl_checks = " AND ".join(f"CONTAINS({_DDL}, '{f}')" for f in PART_B_PRESENT)
_ddl_absent = " AND ".join(f"NOT CONTAINS({_DDL}, '{f}')" for f in PART_B_ABSENT)
_col_in = ", ".join(f"'{c}'" for c, _ in COLUMNS)
PART_B = f"""\
-- PART B -- V165 verify (READ-ONLY). Every CHECK should read OK; paste the grids back.
-- V165.1 / V165.2 now.
SELECT 'V165.1 DAILY_DIGEST has the 6 grounding columns' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'DAILY_DIGEST'
               AND COLUMN_NAME IN ({_col_in})) = 6,
           'OK', 'FAIL: a V165 column is missing') AS RESULT
UNION ALL
SELECT 'V165.2 SP_DAILY_DIGEST is the V165 proc',
       IFF({_ddl_checks}
           AND {_ddl_absent},
           'OK', 'FAIL: SP_DAILY_DIGEST is not the V165 text');

-- V165.3 the next morning, after the 07:20 CT run. expect BODY_SOURCE AI (GROUNDING_OK TRUE, FIGURES_CHECKED >= 0) or
--        TEMPLATE (GROUNDING_OK FALSE with UNGROUNDED listed, or NULL when Cortex failed), and FACTS filled in. Then
--        confirm in Teams that the morning card arrived.
SELECT DIGEST_DATE, MODEL, BODY_SOURCE, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, LEFT(FACTS, 300) AS FACTS_START,
       LEFT(BODY, 200) AS BODY_START, CREATED_AT
  FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST
 ORDER BY DIGEST_DATE DESC
 LIMIT 1;

-- V165.3b expect STATE SUCCEEDED (ACCOUNT_USAGE, up to ~45 min behind). RETURN_VALUE stays NULL (a task that CALLs a
--         proc does not publish the proc's 'digest written (...)' string); V165.3 above is the evidence.
SELECT SCHEDULED_TIME, STATE, RETURN_VALUE, ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH' AND NAME = 'TASK_DAILY_DIGEST'
   AND SCHEDULED_TIME >= DATEADD('day', -2, CURRENT_TIMESTAMP())
 ORDER BY SCHEDULED_TIME DESC;

-- V165.3c expect SUCCESS for each digest route around 07:20 CT today; the card should now render in Teams.
SELECT CONVERT_TIMEZONE('America/Chicago', CREATED)::TIMESTAMP_NTZ AS CREATED_CT, INTEGRATION_NAME, STATUS,
       LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(
           START_TIME => DATEADD('day', -1, CURRENT_TIMESTAMP()), RESULT_LIMIT => 10000))
 WHERE INTEGRATION_NAME IN (SELECT r.INTEGRATION_NAME FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
                             WHERE r.ENABLED AND r.DELIVER_DIGEST AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL')
   AND HOUR(CONVERT_TIMEZONE('America/Chicago', CREATED)) = 7
 ORDER BY CREATED DESC;
"""

pf = os.environ.get("PREFLIGHT_OUT")
if pf:
    Path(pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {pf}")
pb = os.environ.get("PART_B_OUT")
if pb:
    Path(pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {pb}")

print(f"V165 written: {target} ({len(out)} bytes)")
